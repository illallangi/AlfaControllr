#!/usr/bin/env python3

import base64
import distutils.util
import hashlib
import jinja2
import kubernetes
import logging
import os
import schedule
import six
import time
import yaml
import jmespath

from netaddr import IPAddress
from subprocess import PIPE, run

DEBUG           = bool(distutils.util.strtobool(os.environ.get('DEBUG', 'no')))
OWNERREFERENCES = bool(distutils.util.strtobool(os.environ.get('OWNERREFERENCES', 'yes')))
MANAGEDBY       = os.environ.get('MANAGEDBY')
CONTROLLERS     = os.environ.get('CONTROLLERS')
LOGLEVEL        = os.environ.get('LOGLEVEL', 'INFO').upper()
INTERVAL        = float(os.environ.get('INTERVAL', 15))

def main():
  logging.basicConfig(format='%(asctime)s %(levelname)s: %(message)s', level=LOGLEVEL)
  logging.getLogger('schedule').setLevel(logging.ERROR)

  hashes={}
  schedule.every(INTERVAL).seconds.do(tick, hashes)
  schedule.run_all()
  while INTERVAL > 0 and not DEBUG:
    time.sleep(1)
    schedule.run_pending()

def tick(hashes):
  try:
    if 'KUBERNETES_SERVICE_HOST' in os.environ:
      kubernetes.config.load_incluster_config()
      logging.info(f'Loaded k8s config from cluster')
    else:
      kubernetes.config.load_kube_config()
      logging.info(f'Loaded k8s config from kubeconfig')
  except kubernetes.config.ConfigException as e:
    logging.error(f'Cannot initialize kubernetes API, terminating.')
    exit()
  
  coreV1Api = kubernetes.client.CoreV1Api()
  ### FIXME: Disabled until Kubernetes Python Client Library v12 is out
  ### https://github.com/kubernetes-client/python/issues/1172
  #apiextensionsV1Api = kubernetes.client.ApiextensionsV1Api()
  ### END FIXME
  customObjectsApi = kubernetes.client.CustomObjectsApi()
  j2environment = jinja2.Environment(loader=jinja2.BaseLoader, extensions=['jinja2_ansible_filters.AnsibleCoreFiltersExtension'])
  # add b64decode filter to jinja2 env
  j2environment.filters['b64decode'] = base64.b64decode
  j2environment.filters['ipaddr'] = ipaddr
  j2environment.filters['json_query'] = json_query
  j2environment.filters['unique_dict'] = unique_dict
  j2environment.tests['is_subset'] = is_subset
  j2environment.tests['is_superset'] = is_superset
  
  # Retrieve Namespaces
  logging.info('Retrieving Namespaces:')
  try:
    nss = (coreV1Api.list_namespace().items or [])
  except kubernetes.client.rest.ApiException as e:
    logging.warning('   - Unable to retrieve namespaces, aborting')
    logging.info(e)
    return
  
  # Retrieve Secrets
  logging.info('Retrieving Secrets:')
  try:
    secrets = (coreV1Api.list_secret_for_all_namespaces().items or [])
  except kubernetes.client.rest.ApiException as e:
    logging.warning('   - Unable to retrieve secrets, aborting')
    logging.info(e)
    return
  
  # Retrieve Services
  logging.info('Retrieving Services:')
  try:
    services = (coreV1Api.list_service_for_all_namespaces().items or [])
  except kubernetes.client.rest.ApiException as e:
    logging.warning('   - Unable to retrieve services, aborting')
    logging.info(e)
    return
  
  controllers = []  
  if CONTROLLERS:
    with open(os.path.realpath(CONTROLLERS), 'r') as file:
      yamlControllers = yaml.load(file, Loader=yaml.FullLoader)
      if yamlControllers['apiVersion'] == "v1beta3" and yamlControllers['kind'] == "List":
        for obj in yamlControllers.get('items') or []:
          if not obj['kind']=='AlfaControllr': continue
          controllers.append(obj)
      if yamlControllers['apiVersion'] == "controllers.illallangi.enterprises/v1beta" and yamlControllers['kind'] == "AlfaControllr":
        controllers.append(yamlControllers)
    logging.info(f'Loaded {len(controllers)} Alfa Controllrs from {os.path.realpath(CONTROLLERS)}')
    for controller in controllers:
      logging.debug(f' - {controller["metadata"]["name"]}')
  else:
    objs = []
    try:
      objs = (customObjectsApi.list_cluster_custom_object('controllers.illallangi.enterprises', 'v1beta', 'alfacontrollrs').get('items') or [])
    except kubernetes.client.rest.ApiException as e:
      logging.error(f'Unable to get Alfa Controllrs ({e.reason})')
    for obj in objs:
      if not obj['kind']=='AlfaControllr': continue
      controllers.append(obj)
    logging.info(f'Loaded {len(controllers)} Alfa Controllrs from Kubernetes API')
  
  for controller in controllers:
    logging.info(f'Alfa Controllr "{controller["metadata"]["name"]}":')
    
    # Create empty objects array
    objects = []
    
    if (controller['spec'].get('core') or {}).get('namespace') or False:
      for ns in nss:
        obj = coreV1Api.read_namespace(ns.metadata.name)
        objects.append(obj.to_dict())
    
    if (controller['spec'].get('core') or {}).get('secret') or False:
      for secret in secrets:
        obj = coreV1Api.read_namespaced_secret(secret.metadata.name, secret.metadata.namespace)
        objects.append(obj.to_dict())
    
    if (controller['spec'].get('core') or {}).get('service') or False:
      for service in services:
        obj = coreV1Api.read_namespaced_service(service.metadata.name, service.metadata.namespace)
        objects.append(obj.to_dict())
    
    # Retrieve CustomResourceDefinitions
    logging.info(' - Retrieving CustomResourceDefinitions')
    crds = (controller['spec'].get('crds') or [])
    
    # Retrieve Custom Resources
    if len(crds) > 0:
      for crdName in crds:
        logging.info(f' - Retrieving {crdName} Custom Resource Definition:')
        #crd = apiextensionsV1Api.read_custom_resource_definition(crdName)
        ### FIXME: Hard Coding this nonsense until Kubernetes Python Client Library v12 is out
        ### https://github.com/kubernetes-client/python/issues/1172
        crd = {
          'group': crdName.split('.',1)[1],
          'versions': [
            {
              'name': 'v1beta'
            }
          ],
          'names': {
            'plural': crdName.split('.',1)[0]
          }
        }
        ### END FIXME
        logging.info(f' - Retrieving {crd["names"]["plural"]}:')
        for ns in nss:
          try:
            objs = (customObjectsApi.list_namespaced_custom_object(crd['group'], crd['versions'][0]['name'], ns.metadata.name, crd['names']['plural']).get('items') or [])
          except kubernetes.client.rest.ApiException as e:
            logging.error(f'Alfa Controllr "{controller["metadata"]["name"]}" unable to get "{crd["names"]["plural"]}" ({e.reason}) in {ns.metadata.name}, skipping this namespace')
            continue
          for obj in objs:
            objects.append(obj)
    
    if len(objects) == 0:
      logging.warning(f' - 0 objects found, aborting')
      continue
    
    objectsHash = hashlib.sha256((yaml.dump(objects) + yaml.dump(controller['metadata']) + yaml.dump(controller['spec'])).encode('utf-8')).hexdigest()
    if objectsHash == (hashes.get(controller["metadata"]["name"]) or ''):
      logging.info(f' - {len(objects)} found objects have unchanged hash {objectsHash}, aborting')
      continue
    hashes[controller["metadata"]["name"]] = objectsHash
    
    logging.info(f' - {len(objects)} found objects have changed hash {objectsHash}, applying')
    template = controller['spec']['template']
    logging.info(f'Applying template')

    try:
      j2template = j2environment.from_string(source=template)
      j2result = j2template.render(objects=objects, controller=controller, ownerReferences=OWNERREFERENCES, managedBy=MANAGEDBY)
    except jinja2.exceptions.TemplateError as e:
      logging.error(f'Alfa Controllr "{controller["metadata"]["name"]}" unable to render template ({e}), aborting')
      hashes[controller["metadata"]["name"]] = ''
      continue

    try:
      renders = list(yaml.load_all(j2result, Loader=yaml.FullLoader))
    except (yaml.parser.ParserError,yaml.scanner.ScannerError) as e:
      logging.error(f'Alfa Controllr "{controller["metadata"]["name"]}" unable to load rendered template ({e}), aborting')
      hashes[controller["metadata"]["name"]] = ''
      continue
    
    for render in renders:
      try:
        body = yaml.dump(render)
      except yaml.parser.ParserError as e:
        logging.error(f'Alfa Controllr "{controller["metadata"]["name"]}" unable to dump loaded and rendered template ({e}), aborting')
        hashes[controller["metadata"]["name"]] = ''
        continue
      if DEBUG or False:
        print('---')
        print(body)
      else:
        run(["kubectl", "apply", "-f-"], input=body.encode())

  print()

# Get around pyyaml removing leading 0s
# https://github.com/yaml/pyyaml/issues/98
def string_representer(dumper, value):
  if value.startswith("0"):
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style="'")
  return dumper.represent_scalar("tag:yaml.org,2002:str", value)
yaml.Dumper.add_representer(six.text_type, string_representer)

def json_query(v, f):
  return jmespath.search(f, v)

def unique_dict(v):
  return list(yaml.load(y, Loader=yaml.FullLoader) for y in set(yaml.dump(d) for d in v))

def is_superset(v, subset):
  return is_subset(subset, v)

# https://stackoverflow.com/posts/18335110/timeline
# cc-by-sa 4.0
def is_subset(v, superset):
  try:
    for key, value in v.items():
      if type(value) is dict:
        result = is_subset(value, superset[key])
        assert result
      else:
        assert superset[key] == value
        result = True
  except (AssertionError, KeyError):
    result = False
  return result

def ipaddr(value, action):
  if action == "revdns":
    return IPAddress(value).reverse_dns.strip('.')
  raise NotImplementedError

if __name__ == "__main__":
  main()

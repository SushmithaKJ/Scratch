import requests
import json
import urllib2
import requests.packages.urllib3 as urllib3
from requests import Session
from jinja2 import Environment, FileSystemLoader
import os
from jsonschema import validate
import time
import re

requests.packages.urllib3.disable_warnings()

# Globals
DEPLOYMENT_SETTINGS_SCHEMA = 'deployment_settings_schema.json'

class AWX(object):
    def __init__(self, endpoint, token):
        self.session = Session()
        self.session.headers.update({
            'Authorization': 'Bearer ' + token
        })
        self.session.verify = False
        self.uri = 'https://' + endpoint + '/api/v2/'
        self.jinja = Environment(
            loader=FileSystemLoader('templates'),                                                                                                                            
            trim_blocks=True,
        )
        def get_env(value, key):
            return os.getenv(key, value)
        self.jinja.filters['get_env'] = get_env

    def test_connectivity(self):
        resp = self.session.get(self.uri + 'ping')
        if resp.status_code != 200:
            return {'status': 'error', 'message': 'Unable to reach AWX with provided credentials'}
        return {'status': 'success', 'message': 'Connectivity verified'}

    def get_credential(self, name):
        resp = self.session.get(self.uri + 'credentials/?name__iexact=' + urllib2.quote(name))
        if resp.status_code == 200:
            return {'status':'exists', 'credential': resp.json()['results'][0]} if resp.json()['count'] == 1 else {'status': 'unknown'}
        return {'status': 'unknown'}

    def fetch_credentials(self):
        resp = self.session.get(self.uri + 'credentials')
        if resp.status_code == 200:
            return_items = []
            for result in resp.json()['results']:
                return_items.append({'label': result['name'],'value': result['name']})
            return {'status': 'success', 'message': 'Successfully returning fetched credentials', 'data': return_items}
        return {'status': 'error', 'message': 'An error occurred while trying to retrieve credentials'}
    
    def get_template(self, name):
        resp = self.session.get(self.uri + 'job_templates/?name__iexact=' + urllib2.quote(name))
        if resp.status_code == 200:
            return {'status':'exists', 'template': resp.json()['results'][0]} if resp.json()['count'] == 1 else {'status': 'unknown'}
        return {'status': 'unknown'}

    def fetch_templates(self,nameOnly=False):
        resp = self.session.get(self.uri + 'job_templates')
        if resp.status_code == 200:
            return_items = []
            for result in resp.json()['results']:
                return_items.append({'label': result['name'],'value': result['name']})
            return {'status': 'success', 'message': 'Successfully returning fetched templates', 'data': return_items}
        return {'status': 'error', 'message': 'An error occurred while trying to retrieve job templates'}

    def get_inventory(self, name):
        resp = self.session.get(self.uri + 'inventories/?name__iexact=' + urllib2.quote(name))
        if resp.status_code == 200:
            return {'status':'exists', 'inventory': resp.json()['results'][0]} if resp.json()['count'] == 1 else {'status': 'unknown'}
        return {'status': 'unknown'}

    def launch_template(self,id,extra_vars=None, credentials=None, inventory=None):
        req_payload={}
        if extra_vars:
            req_payload['extra_vars'] = self.render(extra_vars)
        if credentials:
            req_payload['credentials'] = credentials
        if inventory:
            req_payload['inventory_id'] = inventory
        print json.dumps(req_payload,indent=4)
        return self.session.post(self.uri + 'job_templates/{}/launch/'.format(id), json=req_payload)

    def render(self, message):
        message = json.dumps(message)
        matches = re.findall(r'_ENV_\w+', message)
        for match in matches:
            env_variable = '_'.join(match.split('_')[2:])
            env_variable = '{' + "{{ 'default' | get_env('{env_var}') }}".format(env_var=env_variable.upper()) + '}'
            message = re.sub(match, env_variable, message)
        return json.loads(self.jinja.from_string(message).render())

    def validate_deployment_settings(self, deployment_settings):
        # Validate deployment_settings against schema
        validated_settings = []
        # try:
        #     schema = {}
        #     with open(DEPLOYMENT_SETTINGS_SCHEMA, 'r') as f:
        #         schema = json.load(f)
        #     validate(deployment_settings, schema)
        # except Exception as e:
        #     print e
        #     e = str(e).split('\n')
        #     return {'status': 'error', 'message': "{} {}".format(e[2], e[0])}
        # Verify job template exists
        for setting in deployment_settings:
            resp = self.get_template(setting['template_name'])
            if resp['status'] == 'unknown':
                return {'status': 'error', 'message': 'Unknown template name: {}'.format(setting['name'])}
            template = resp['template']
            current_setting = {
                'template_name': setting['template_name'],
                'template_id': template['id'],
                'credentials': [],
                'inventory': [],
                'count': setting['run_count'],
                'wait': setting['wait'] if 'wait' in setting else "yes"
            }
            if 'credential_list' in setting:
                if not template['ask_credential_on_launch']:
                    return {'status': 'error', 'message': 'Credentials are not allowed to be passed for template: {}'.format(setting['name'])}
                for credential in setting['credential_list']:
                    resp = self.get_credential(credential)
                    if resp['status'] == 'unknown':
                        return {'status': 'error', 'message': 'Unknown credential name: {}'.format(credential)}
                    current_setting['credentials'].append(resp['credential']['id'])
            else:
                del current_setting['credentials']
            if 'inventory' in setting:
                if not template['ask_inventory_on_launch']:
                    return {'status': 'error', 'message': 'Inventory is not allowed to be passed for template: {}'.format(setting['name'])}
                if '_ENV_' not in setting['inventory']:
                    resp = self.get_inventory(setting['inventory'])
                    if resp['status'] == 'unknown':
                        return {'status': 'error', 'message': 'Unable to find inventory for: {}'.format(setting['inventory'])}
                    current_setting['inventory'] = resp['inventory']['id']
            else:
                del current_setting['inventory']
            current_setting['pass_extra_vars'] = template['ask_variables_on_launch']
            validated_settings.append(current_setting)
        return {'status': 'success', 'settings': validated_settings, 'message': 'Validation Successful'}

    def run_deployment(self, templates, extra_vars):
        for template in templates:
            jobs = []
            for i in range(template['count']):
                extra_vars['workflow_vars']['loop_index'] = i
                resp = self.launch_template(
                    id=template['template_id'],
                    extra_vars=self.render(extra_vars) if template['pass_extra_vars'] else None,
                    credentials=template['credentials'] if 'credentials' in template else None,
                    inventory=template['inventory'] if 'inventory' in template else None
                )
                if resp.status_code == 201:
                    jobs.append(resp.json()['id'])
                else:
                    print resp.status_code
                    return {'status': 'error', 'message': 'Failed to launch job for: {} count: {}'.format(template['template_name'], i)}
            if template['wait'] == "yes":
                resp = self.wait_on_jobs(jobs)
                if resp['status'] != 'success':
                    return resp
        return {'status': 'success'}
    
    def wait_on_jobs(self, jobs, timeout=1200):
        run_time = 0
        start = time.time()
        while True and run_time < timeout:
            # print '\n------------------------------------------------------------------\nElapsed Time:{}s\n'.format(int(time.time() - start))
            resp = self.session.get(self.uri + 'jobs/?id__in=' + ','.join(str(x) for x in jobs))
            if resp.status_code == 200:
                pending = False
                for job in resp.json()['results']:
                    print 'Status for job: {} is {}'.format(job['id'],job['status'])
                    if job['failed']:
                        return {'status': 'error', 'message': 'Job {} failed to complete'.format(job['id'])}
                    elif job['status'] != 'successful':
                        pending = True
                if not pending:
                    return {'status': 'success'}
            else:
                return {'status': 'error', 'message': 'An error occurred whle getting logs'}
            time.sleep(10)
            run_time = int(time.time() - start)
        return {'status': 'error', 'message': 'A timeout occurred while waiting for jobs'}

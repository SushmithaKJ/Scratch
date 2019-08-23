import json
import os
import threading

import kafka.errors
import pymongo
import pymongo.errors
import requests
import yaml
from kafka import KafkaConsumer
from pymongo import MongoClient

# absolute import
# from consumer.apic import APIC
# from consumer.logger import Logger
# from consumer.database import Database

# relative imports
from .apic import APIC
from ..logger import Logger
from .database import Database


class AuroraThread(threading.Thread):
    """Base thread class; initializes logging and exit conditions"""

    def __init__(self, exit, lock):
        super(AuroraThread, self).__init__()
        self.logger = Logger.get_logger()
        self.exit = exit
        self.lock = lock


class APICThread(AuroraThread):
    """Logs into APIC and refreshes session cookies every 270 seconds"""

    def __init__(self, exit, lock):
        super(APICThread, self).__init__(exit, lock)

    def run(self):
        with self.lock:
            apic = APIC.get_apic()

        login_response = apic.login()
        if login_response.status_code != 200:
            apic.logger.error('Unable to login into APIC.  Error: {}'.format(login_response.text))
            apic.logger.error("Shutting down")
            self.exit.set()
            return  # TODO figure out how to possibly retry etc

        self.exit.wait(270) # initial login just occurred, no need to refresh immediately; essentially a do-while
        while not self.exit.is_set():
            apic.refresh()
            self.exit.wait(270)

        apic.logout()
        apic.close()
        self.logger.info("APIC thread exited succesfully")


class ConsumerThread(AuroraThread):
    """Class for consumer thread.

    Set up APIC, Kafka consumer, and database and wait for messages on Kafka
    Take each message and process them based on message type
    """

    def __init__(self, exit, lock):
        super(ConsumerThread, self).__init__(exit, lock)

    def set_config(self):
        # Reading configuration from environment variables
        self.config = {}
        self.config['tenant'] = os.getenv('TENANT_NAME')
        self.config['application_profile'] = os.getenv('AP_NAME')
        self.config['mongo_host'] = os.getenv('MONGO_HOST')
        self.config['mongo_port'] = int(os.getenv('MONGO_PORT'))
        self.config['kafka_topic'] = os.getenv('KAFKA_OUTPUT_TOPIC')
        self.config['kafka_ip'] = os.getenv('KAFKA_HOSTNAME')

    def run(self):
        self.logger.info("Reading configuration from file")
        self.set_config()

        try:
            self.logger.info("Connecting to Kafka server")
            self.consumer = KafkaConsumer(self.config['kafka_topic'], bootstrap_servers=self.config['kafka_ip'], auto_offset_reset='earliest')
            self.logger.info("Successfully connected to Kafka")

            self.logger.info("Connecting to MongoDB")
            self.db = Database(host=self.config['mongo_host'], port=self.config['mongo_port'])
            self.logger.info("Successfully connected to MongoDB")
        except (kafka.errors.NoBrokersAvailable, pymongo.errors.ConnectionFailure) as error:
            self.logger.error(str(error))
            self.exit.set()
            self.logger.info("Consumer thread exiting")
            return

        with self.lock:
            self.apic = APIC.get_apic()

        self.logger.info("Waiting for messages from Kafka")
        while not self.exit.is_set():
            msg_pack = self.consumer.poll()
            for tp, messages in msg_pack.items():
                for msg in messages:
                    self.logger.debug("Received message from Kafka")
                    self.process_message(msg)
                    if self.exit.is_set():
                        break
            # self.consumer.commit_async()
            # TODO figure out if need for commits, no need if not using consumer group_id
        self.logger.info("Closing Kafka consumer")
        self.consumer.close()
        self.logger.info("Consumer thread exited succesfully")

    def process_message(self, msg):
        """Take msg as string and get dictionary
        Do basic key/value processing and call appropriate function for message type
        """
        props = json.loads(msg.value)
        props['_id'] = props['uuid']
        status = props['status']
        msg_type = props['msg_type']
        del props['uuid'], props['status'], props['msg_type']

        if msg_type == 'ep':
            self.logger.debug("Received endpoint message")
            self.process_endpoint_message(props, status)
        elif msg_type == 'epg':
            self.logger.debug("Received grouping message")
            self.process_grouping_message(props, status)
        elif msg_type == 'contract':
            self.logger.debug("Received contract message")
            self.process_contract_message(props, status)
        else:
            self.logger.error("Received invalid message type")

    def process_endpoint_message(self, props, status):
        """Process endpoint message"""
        if status == 'create' or status == 'update':

            endpoint = self.db.get_endpoint(props['_id'])
            if not endpoint:
                self.db.insert_endpoint(props)
                self.logger.debug("Added endpoint {} to DB".format(props['name']))
            else:
                self.db.update_endpoint(props['_id'], {'$set': props})
                self.logger.debug("Updated endpoint {}".format(props['name']))
        elif status == 'delete':
            endpoint = self.db.get_endpoint(props['_id'])
            self.db.delete_endpoint(props['_id'])
            self.logger.debug("Deleted endpoint {}".format(props['name']))
            self.db.update_epg_membership(endpoint['epg'], [props['_id']], remove=True)
            self.logger.debug("Removed endpoint {} from EPG {} in DB".format(props['name'], endpoint['epg']))
        else:
            pass # TODO possibly error handle if msg is bad?

    def process_grouping_message(self, props, status):
        """Process grouping message"""
        tenant = self.config['tenant']
        ap = self.config['application_profile']

        if status == 'create' or status == 'update':
            epg = self.db.get_epg(props['_id']) # get existing app state for EPG

            # Store endpoint membership with EP uuid instead of sys_id
            # REVIEW currently checks for incorrect message order, not needed if msg source is reliable
            sysid_to_uuid = lambda x: self.db.get_endpoint(x, identifier='sys_id')['_id']
            try:
                props['members'] = list(map(sysid_to_uuid, props['members']))
            except KeyError as error:
                self.logger.warning("EPG created before member EPs")

            if not epg: # no previous state; creation of new EPG
                self.create_epg(tenant, ap, props['name'])
                self.logger.debug("Created EPG {} on APIC".format(props['name']))
                props['consumed'] = []
                props['provided'] = []
                self.db.insert_epg(props)
                self.logger.debug("Added endpoint {} to DB".format(props['name']))
            else: # update existing EPG

                # TODO change EPG setting on APIC if applicable
                # pseudocode
                # if epg_info_changed():
                #     make_apic_call() # for now, EPG is defined by name only, no changes to make
                #     update_db_entry()

                # update membership in database
                # TODO update config on APIC, currently app has no concept of EPs on APIC
                prev = set(epg['members'])
                update = set(props['members'])
                if prev != update:
                    self.logger.debug("Updating {} membership".format(props['name']))
                    removed = prev - update
                    added = update - prev
                    self.db.update_epg_membership(props['_id'], props['members'])
                    self.logger.debug("Updated EPG {} in DB".format(props['name']))

                    # TODO update endpoints on APIC, using `removed` and `added`
                    for id in removed:
                        self.db.update_endpoint(id, {'$set': {'epg': ''}})
                        self.logger.debug("Removed grouping for endpoint {}".format(id))
                    for id in added:
                        endpoint = self.db.get_endpoint(id)
                        if endpoint['epg'] != props['_id']: # disassociate EP from previous EPG
                            self.db.update_epg_membership(endpoint['epg'], [id], remove=True)
                            self.logger.debug("Removed {} from EPG {}".format(id, endpoint['epg']))
                            self.db.update_endpoint(id, {'$set': {'epg': props['_id']}})
                        self.logger.debug("Updated grouping for endpoint {}".format(id))

        elif status == 'delete':
            self.create_epg(tenant, ap, props['name'], delete=True)

            # remove EPG from related contract DB entries
            epg = self.db.get_epg(props['_id'])
            for contract in epg['consumed']:
                self.db.update_contract_membership(contract, 'consumed', [props['id']], remove=True)
            for contract in epg['provided']:
                self.db.update_contract_membership(contract, 'provided', [props['id']], remove=True)
            self.logger.debug("Removed contracts from EPG {}".format(props['name']))

            # TODO possibly modify endpoint entries in DB
            for ep in epg['members']:
                self.db.update_endpoint(ep, {'$set': {'epg': ''}})
            self.logger.debug('Updated endpoint grouping entries in DB')

            self.db.delete_epg(props['_id'])
            self.logger.debug("Deleted endpoint {} in DB".format(props['name']))
        else:
            pass # TODO possibly error handle if msg is bad

    def process_contract_message(self, props, status):
        tenant = self.config['tenant']
        ap = self.config['application_profile']

        if status == 'create' or status == 'update':

            # TODO determine if/how/what filter info will contain
            if 'filter_entries' not in props: # TODO figure out filter_entries, current messages missing field
                props['filter_entries'] = "ANY"
            if 'action' not in props:
                props['action'] = 'deny'

            # find filter
            if props['filter_entries'] == "ANY":
                filter_name = tenant + "-any"
            else:
                # REVIEW current naming scheme below limited; filter names on apic limited to 64 characters
                # TODO revise naming below depending on msg format for filter entries
                filter_name = "-".join([tenant] + list(map(str, props['filter_entries'])))

            filter = self.db.get_filter(filter_name)
            if not filter: # create filter
                self.create_filter(tenant, filter_name, props['filter_entries'])
                self.db.insert_filter(filter_name, tenant, props['filter_entries'])

            props['filter_name'] = filter_name

            # REVIEW what is `action` options in msg
            if props['action'] == 'ALLOW':
                props['action'] = 'permit'

            # REVIEW temporary solution as currently cons/prov are not lists but str for single EPG
            # currently processing with lists, easier to expand later
            props['consumer_epg'] = [props['consumer_epg']]
            props['provider_epg'] = [props['provider_epg']]
            ####

            # create new contract with above filter
            # REVIEW below snow is meant to work with multi-cons/prov contracts
            # i.e. `type(kafka_msg['consumer_epg']) == list`

            # `contract` represents old entry in DB
            # `props` represents new to be updated entry
            contract = self.db.get_contract(props['_id'], identifier='id')
            if not contract:
                self.create_contract(tenant, props['name'], filter_name, props['action'])
                self.db.insert_contract(props)
                contract = props

                # add consumer/provider fields
                added_cons = contract['consumer_epg']
                added_prov = contract['provider_epg']
                removed_cons = []
                removed_prov = []

            else:
                # update filter entry
                if filter_name != contract['filter_name']:
                    self.change_contract_filter(tenant, props['name'], filter_name, contract['filter_name'], props['action'])
                    self.db.update_contract_filter(props['_id'], filter_name, props['filter_entries'])

                # update consumer/provider
                self.db.update_contract_membership(props['_id'], 'consumed', props['consumer_epg'])
                self.db.update_contract_membership(props['_id'], 'provided', props['provider_epg'])
                self.logger.debug("Determining contract cons/prov updates")
                prev_cons = set(contract['consumer_epg'])
                update_cons = set(props['consumer_epg'])
                added_cons = list(update_cons - prev_cons)
                removed_cons = list(prev_cons - update_cons)

                prev_prov = set(contract['provider_epg'])
                update_prov = set(props['provider_epg'])
                added_prov = list(update_prov - prev_prov)
                removed_prov = list(prev_prov - update_prov)

            # attach contract consumer/producer on APIC
            # associate contracts to EPG entries in database
            # assumes epgs already created
            if added_cons:
                for epg_id in added_cons:
                    consumer = self.db.get_epg(epg_id)
                    self.attach_contract('consumed', tenant, ap, consumer['name'], contract['name'])
                self.db.add_contract('consumed', added_cons, contract['_id'])

            if removed_cons:
                for epg_id in removed_cons:
                    consumer = self.db.get_epg(epg_id)
                    self.attach_contract('consumed', tenant, ap, consumer['name'], contract['name'], delete=True)
                self.db.remove_contract('consumed', removed_cons, contract['_id'])

            if added_prov:
                for epg_id in added_prov:
                    provider = self.db.get_epg(epg_id)
                    self.attach_contract('provided', tenant, ap, provider['name'], contract['name'])
                self.db.add_contract('provided', added_prov, contract['_id'])

            if removed_prov:
                for epg_id in removed_prov:
                    provider = self.db.get_epg(epg_id)
                    self.attach_contract('provided', tenant, ap, provider['name'], contract['name'], delete=True)
                self.db.remove_contract('provided', removed_prov, contract['_id'])

        elif status == 'delete':
            self.create_contract(tenant, props['name'], filter=None, action=None, delete=True)
            contract = self.db.get_contract(props['_id'], identifier='id')

            self.db.remove_contract('consumed', contract['consumer_epg'], props['_id'])
            self.db.remove_contract('provided', contract['provider_epg'], props['_id'])
            self.db.delete_contract(props['_id'])
        else:
            pass # TODO possibly error handle if msg is bad?


    def attach_contract(self, role, tenant, ap, epg, contract, status='created,modified', delete=False):
        """Attach a contract to an EPG
        Create fvRsCons or fvRsProv on APIC

        :param role: str
            `'consumed'` or `'provided'`
        :param tenant: str
        :param ap: str
        :param epg: str
        :param contract: str
        :param status: str
            `'created,modified'` or `'deleted'`
        :return:
        """
        assert role == 'consumed' or role == 'provided'
        assert status == 'created,modified' or 'deleted'
        if role == 'consumed':
            class_ = 'fvRsCons'
        elif role == 'provided':
            class_ = 'fvRsProv'

        status = 'deleted' if delete else 'created,modified'

        debug_action = 'Setting' if status == 'created,modified' else 'Deleting'
        role_log = 'consumer' if role == 'consumed' else 'provider'
        self.logger.debug("{} EPG '{}' as {} for contract '{}' on APIC".format(debug_action, epg, role_log, contract))

        url = "/api/node/mo/uni/tn-{}/ap-{}/epg-{}.json".format(tenant, ap, epg)
        payload = {
          class_: {
            "attributes": {
              "tnVzBrCPName": contract,
              "status": status
            }
          }
        }
        res = self.apic.request("POST", url, data=json.dumps(payload))
        if res.status_code == 200:
            self.logger.debug("Successfully processed contract '{}' for EPG '{}'".format(contract, epg))


    def create_epg(self, tenant, ap, epg, status='created,modified', delete=False):
        """Make an endpoint group
        Create fvAEPg on APIC

        :param tenant: str
        :param ap: str
        :param epg: str
        :param status: str
            `'created,modified'` or `'deleted'`
        :return:
        """
        assert status == 'created,modified' or 'deleted'

        status = 'deleted' if delete else 'created,modified'
        debug_action = 'Creating' if status == 'created,modified' else 'Deleting'
        self.logger.debug("{} EPG '{}' on APIC".format(debug_action, epg))

        url = "/api/node/mo/uni/tn-{}/ap-{}/epg-{}.json".format(tenant, ap, epg)
        # self.logger.debug("Creating EPG: {}".format(epg))
        payload = {
          "fvAEPg": {
            "attributes": {
              "name": epg,
              "status": status
            }
          }
        }
        res = self.apic.request("POST", url, data=json.dumps(payload))
        if res.status_code == 200:
            self.logger.debug("Successfully processed EPG '{}'".format(epg))

    def create_contract(self, tenant, contract, filter, action, status='created,modified', delete=False):
        """Make a contract
        Create vzBrCP and vzSubj on APIC

        :param tenant: str
        :param contract: str
        :param filter: str
        :param status: str
            `'created,modified'` or `'deleted'`
        :return:
        """
        assert status == 'created,modified' or 'deleted'
        status = 'deleted' if delete else 'created,modified'
        debug_action = 'Creating' if status == 'created,modified' else 'Deleting'
        self.logger.debug("{} contract '{}' on APIC".format(debug_action, contract))

        url = "/api/node/mo/uni/tn-{0}/brc-{1}.json".format(tenant, contract)

        payload = {
          "vzBrCP": {
            "attributes": {
              "name": contract,
              "status": status
            },
            "children": [
              {
                "vzSubj": {
                  "attributes": {
                    "name": "contract-subject",
                    "status": status
                  }
                }
              }
            ]
          }
        }
        if status != "deleted": # REVIEW or maybe if filter == None? maybe want to delete filter sometimes
            filt_att = [
              {
                "vzRsSubjFiltAtt": {
                  "attributes": {
                    "status": status,
                    "tnVzFilterName": filter,
                    "directives": "none",
                    "action": action.lower()
                  }
                }
              }
            ]
            payload["vzBrCP"]["children"][0]['vzSubj']["children"] = filt_att

        res = self.apic.request('POST', url, data=json.dumps(payload))
        if res.status_code == 200:
            self.logger.debug("Successfully processed contract '{}'".format(contract))

    def change_contract_filter(self, tenant, contract, new_filter, old_filter, action):
        url = "/api/node/mo/uni/tn-{0}/brc-{1}/subj-contract-subject.json".format(tenant, contract)
        self.logger.debug("Changing filter for contract {}".format(contract))

        payload = {
            "vzSubj": {
                "attributes": {
                    "name": "contract-subject",
                    "status": "created,modified"
                },
                "children": [
                    {
                      "vzRsSubjFiltAtt": {
                        "attributes": {
                          "status": "deleted",
                          "tnVzFilterName": old_filter,
                          "directives": "none",
                          "action": action.lower(),
                        }
                      }
                    },
                    {
                      "vzRsSubjFiltAtt": {
                        "attributes": {
                          "status": "created,modified",
                          "tnVzFilterName": new_filter,
                          "directives": "none",
                          "action": action.lower()
                        }
                      }
                    }
                ]
            }
        }

        res = self.apic.request('POST', url, data=json.dumps(payload))
        if res.status_code == 200:
            self.logger.debug("Successfully updated filter for contract '{}'".format(contract))

    def create_filter(self, tenant, filter, ports, status='created,modified', delete=False):
        """Make a filter
        Create vzFilter and vzEntry on APIC

        :param tenant: str
        :param filter: str
        :param entries: str
        :param status: str
            `'created,modified'` or `'deleted'`
        :return:
        """
        assert status == 'created,modified' or 'deleted'

        status = 'deleted' if delete else 'created,modified'

        debug_action = 'Creating' if status == 'created,modified' else 'Deleting'
        self.logger.debug("{} filter '{}' on APIC".format(debug_action, filter))

        def entry_payload(port):
            payloads = (
                {
                    "vzEntry": {
                        "attributes": {
                            "name": "tcp_{}".format(port),
                            "etherT": "ip",
                            "prot": "tcp",
                            "dFromPort": port,
                            "dToPort": port,
                            "status": status
                        }
                    }
                },
                {
                    "vzEntry": {
                        "attributes": {
                            "name": "udp_{}".format(port),
                            "etherT": "ip",
                            "prot": "udp",
                            "dFromPort": port,
                            "dToPort": port,
                            "status": status
                        }
                    }
                }
            )
            return payloads

        # create correct filter
        if ports == 'ANY': # REVIEW can we NOT create an entry if we want to allow all for the filter?
            entries = [
              {
                "vzEntry": {
                  "attributes": {
                    "name": "any",
                    "status": status
                  }
                }
              }
            ]
        else:
            entries = [entry for port in ports for entry in entry_payload(port)]

        url = "/api/node/mo/uni/tn-{0}/flt-{1}.json".format(tenant, filter)
        payload = {
          "vzFilter": {
            "attributes": {
              "name": filter,
              "status": status
            },
            "children": entries
          }
        }
        res = self.apic.request('POST', url, data=json.dumps(payload))
        if res.status_code == 200:
            self.logger.debug("Successfully processed filter '{}'".format(filter))

from tetpyclient import RestClient
import tetpyclient
from infoblox_client import connector
import json
import os
import requests.packages.urllib3
import csv
from requests.auth import HTTPBasicAuth
from netaddr import *

requests.packages.urllib3.disable_warnings()

class Pigeon(object):
    def __init__(self):
        self.note = {
            "status_code" : 0,
            "message" : "",
            "data" : {}
        }

    def send(self):
        print json.dumps(self.note)

      
class Tetration_Helper(object):
    class Inventory(object):
        offset = ''
        pagedData = None
        hasNext = False

    def __init__(self, endpoint, api_key, api_secret,pigeon, options):
        self.rc = RestClient(endpoint, api_key=api_key, api_secret=api_secret, verify=False)
        self.scopes = []
        self.pigeon = pigeon
        self.inventory = self.Inventory()
        self.filters = {}
        self.options = options
        self.subnets = []

    def GetSearchDimensions(self):
        resp = self.rc.get('/inventory/search/dimensions')
        return resp.json()

    def GetApplicationScopes(self):
        resp = self.rc.get('/app_scopes')
        if resp.status_code != 200:
            self.pigeon.status_code = '403'
            self.pigeon.note.update({
                'status_code': 403,
                'message' : 'Unable to get application scopes from tetration cluster',
                'data' : {}
            })
            self.pigeon.send()
            exit(0)

        else:
            self.scopes = resp.json()

    def GetInventory(self, filters=None, dimensions=None):
        req_payload = {
            "filter": {
                "type": "or",
                "filters": filters
            },
            "dimensions": dimensions,
            "limit": self.options["limit"],
            "offset": self.inventory.offset if self.inventory else ""
        }
        resp = self.rc.post('/inventory/search',json_body=json.dumps(req_payload))
        if resp.status_code != 200:
            self.pigeon.note.update({
                'status_code': 403,
                'message' : 'Unable to get inventory from tetration cluster',
                'data' : {}
            })
            self.pigeon.send()
            exit(0)
        else:
            self.pigeon.note.update({
                'status_code': 100,
                'message' : 'Successfully retrieved inventory page from Tetration',
                'data' : {}
            })
            self.pigeon.send()
            resp = resp.json()
            self.inventory.pagedData = resp['results']
            self.inventory.offset = resp['offset'] if 'offset' in resp else ''
            self.inventory.hasNext = True if self.inventory.offset else False
            return

    def CreateInventoryFilters(self,network_list):
        inventoryDict = {}
        appScopeId = os.getenv('APP_SCOPE_ID',default='Default')
        for row in network_list:
            if row['comment'] not in inventoryDict:
                inventoryDict[row['comment']] = {}
                inventoryDict[row['comment']]['app_scope_id'] = appScopeId
                inventoryDict[row['comment']]['name'] = row['comment']
                inventoryDict[row['comment']]['primary'] = os.getenv('SCOPE_RESTRICTED',default=False)
                inventoryDict[row['comment']]['query'] = {
                    "type" : "or",
                    "filters" : []
                }
            inventoryDict[row['comment']]['query']['filters'].append({
                "type": "subnet",
                "field": "ip",
                "value": row['network']
            })
        self.filters = inventoryDict
        return

    def PushInventoryFilters(self):
        for inventoryFilter in self.filters:
            req_payload = self.filters[inventoryFilter]
            resp = self.rc.post('/filters/inventories', json_body=json.dumps(req_payload))
        if resp.status_code != 200:
            self.pigeon.note.update({
                'status_code': 403,
                'message' : 'Error pushing inventory filters to tetration cluster',
                'data' : {}
            })
            self.pigeon.send()
            return
        self.pigeon.note.update({
            'status_code': 100,
            'message' : 'Successfully posted inventory filters to Tetration cluster',
            'data' : {}
        })
        self.pigeon.send()
        return

    def AnnotateHosts(self,hosts,columns,csvFile):
        with open(csvFile, "wb") as csv_file:
            fieldnames = ['IP','VRF']
            for column in columns:
                if column["infobloxName"] != 'extattrs':
                    fieldnames.extend([column["annotationName"]])
                else:
                    if column["overload"] == "on":
                        fieldnames.extend([column["annotationName"]])
                    else:
                        for attr in column["attrList"]:
                            fieldnames.extend([str(column["annotationName"]) + '-' + str(attr)])
            writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
            writer.writeheader()
            for host in hosts:
                hostDict = {}
                hostDict["IP"] = host["ip_address"]
                hostDict["VRF"] = [ tetHost["vrf_name"] for tetHost in self.inventory.pagedData if tetHost["ip"] == host["ip_address"] ][0]
                for column in columns:
                    if column["infobloxName"] == 'extattrs':
                        for attr in column["attrList"]:
                            if column["overload"] == "on":
                                if attr in host["extattrs"]:
                                    hostDict[column["annotationName"]] = str(attr) + '=' + str(host["extattrs"][attr]["value"]) + ';' if column["annotationName"] not in hostDict.keys() else hostDict[column["annotationName"]] + str(attr) + '=' + str(host["extattrs"][attr]["value"]) + ';'
                                else:
                                    hostDict[column["annotationName"]] = str(attr) + '=;' if column["annotationName"] not in hostDict.keys() else str(hostDict[column["annotationName"]]) + str(attr) + '=;'
                            else:
                                if attr in host["extattrs"]:
                                    hostDict[column["annotationName"] + '-' + attr] = host["extattrs"][attr]["value"]
                                else:
                                    hostDict[column["annotationName"] + '-' + attr] = ''
                    elif column["infobloxName"] == 'zone':
                        hostDict[column["annotationName"]] = '.'.join(",".join(host["names"]).split('.')[1:])
                    elif column["infobloxName"] == 'names':
                        hostDict[column["annotationName"]] = ",".join(host[column["infobloxName"]]).split('.')[0]
                    else:
                        hostDict[column["annotationName"]] = host[column["infobloxName"]]
                writer.writerow(hostDict)
        keys = ['IP', 'VRF']
        req_payload = [tetpyclient.MultiPartOption(key='X-Tetration-Key', val=keys), tetpyclient.MultiPartOption(key='X-Tetration-Oper', val='add')]
        resp = self.rc.upload(csvFile, '/assets/cmdb/upload', req_payload)
        if resp.status_code != 200:
            self.pigeon.note.update({
                'status_code': 403,
                'message' : 'Error posting annotations to Tetration cluster',
                'data' : {}
            })
            self.pigeon.send()
            return
        else:
            self.pigeon.note.update({
                'status_code': 100,
                'message' : 'Successfully posted annotations to Tetration cluster',
                'data' : {}
            })
            self.pigeon.send()

    def SetSubnets(self,subnets):
        for subnet in subnets:
            self.subnets.append(IPNetwork(subnet))

    def HasSubnetFilterForIp(self,ip):
        for subnet in self.subnets:
            if subnet.__contains__(IPAddress(ip)) is True:
                return True
        return False

class Infoblox_Helper(object):
    def __init__ (self,opts=None,pigeon=None):
        self.infoblox = connector.Connector(opts)
        self.pigeon = pigeon

    def GetHost(self,pagedData):
        host_list = []
        try:
            for host in pagedData:
                host_list.append(self.infoblox.get_object('ipv4address',{'ip_address': host["ip"],'_return_fields': 'network,network_view,names,ip_address,extattrs'}))
                
            return [host[0] for host in host_list if host != None]
        except:
            self.pigeon.note.update({
                'status_code': 303,
                'message' : 'Error getting host from infoblox',
                'data' : {}
            })
            self.pigeon.send()

    def GetSubnet(self,subnet):
        try:
            return self.infoblox.get_object('network',{'network': subnet})
        except:
            self.pigeon.note.update({
                'status_code': 303,
                'message' : 'Error getting subnet from infoblox',
                'data' : {}
            })
            self.pigeon.send()

    def GetExtensibleAttributes(self):
        try:
            return self.infoblox.get_object('extensibleattributedef',{'_return_fields': 'name'})
        except:
            self.pigeon.note.update({
                'status_code': 303,
                'message' : 'Error getting extensible attributes from infoblox',
                'data' : {}
            })
            self.pigeon.send()
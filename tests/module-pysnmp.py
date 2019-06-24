"""
Set up an SNMP Module on top of the Ceph Manager
"""
# We must share a global reference to this instance, because it is the
# gatekeeper to all accesses to data from the C++ side (e.g. the REST API
# request handlers need to see it)
from collections import defaultdict
import collections

_global_instance = {'plugin': None}
def global_instance():
    assert _global_instance['plugin'] is not None
    return _global_instance['plugin']

import os
import platform

import copy
import errno
import json
import math
import random
import six
import time
from mgr_module import MgrModule, MgrStandbyModule, CommandResult
from threading import Event
from mgr_module import CRUSHMap

from types import OsdMap, NotFound, Config, FsMap, MonMap, \
    PgSummary, Health, MonStatus, ServiceMap

from pysnmp.hlapi import *

import rados

class StandbyModule(MgrStandbyModule):
    config = dict()
    ceph_health_mapping = {'HEALTH_OK': 0, 'HEALTH_WARN': 1, 'HEALTH_ERR': 2}
    mon_connection = False
    run = True
    trap_addr = 'localhost'
    trap_port = '162'
    trap_oid = '1.3.6.1.4.1.50495.1.1.0'
    trap_on_start = False
    trap_on_shutdown = False
    sleep_interval = 30
    fsId = 'N/A'
    clusterHealth = None

    def __init__(self, *args, **kwargs):
        super(StandbyModule, self).__init__(*args, **kwargs)
        _global_instance['plugin'] = self
        self.log.error("Initialized parameters Dest={0}:{1}".format(self.trap_addr, self.trap_port))

        self.log.info("Constructing module {0}: instance {1}".format(
            __name__, _global_instance))
        self.event = Event()

    def serve(self):
        module = self
        self.trap_addr = self.get_localized_config('trap_addr', 'localhost')
        self.trap_port = self.get_localized_config('trap_port', '162')
        self.trap_oid = self.get_localized_config('trap_oid', '1.3.6.1.4.1.50495.1.1.0')
        self.sleep_interval = int(self.get_localized_config('sleep_interval', '30'))
        self.trap_on_start = int(self.get_localized_config('trap_on_start', '0'))
        self.trap_on_shutdown = int(self.get_localized_config('trap_on_shutdown', '0'))
        self.log.error("Standby loaded parameters Dest={0} Port={1}".format(self.trap_addr, self.trap_port))
        self.log.error("                          OID ={0} Wait={1}".format(self.trap_oid, self.sleep_interval))
        self.log.error('Starting standby')
        if self.trap_on_start == True:
            timeofday = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
            trapstring = timeofday+" Ceph Manager SNMP Handler - Standby Starting"
            errorIndication, errorStatus, errorIndex, varBinds = next(
                sendNotification(
                    SnmpEngine(),
                    CommunityData('public', mpModel=0),
                    UdpTransportTarget((self.trap_addr, int(self.trap_port))),
                    ContextData(),
                    'trap',
                    NotificationType(
                        #ObjectIdentity('SNMPHANDLER-MIB', 'clusterCheck')
			ObjectIdentity('1.3.6.1.4.1.50495.10.4.0')
                    ).addVarBinds(
                        (ObjectIdentity('SNMPHANDLER-MIB', 'fsId'), OctetString(self.get_fsid())),
                        (ObjectIdentity('SNMPHANDLER-MIB', 'statusDetail'), '3'),
                        (ObjectIdentity('SNMPHANDLER-MIB', 'statusMsg'), OctetString(trapstring))
                    )
                )
            )
        while self.run:
            if (self.have_mon_connection()):
                self.mon_connection = True
            else:
                self.mon_connection = False
                self.send_unknown_trap()
            self.log.error('Ceph Mon Connection is %d', self.mon_connection)
            timeofday = time.strftime('%H%M', time.localtime())
            self.log.warn('Sleeping for %d', self.sleep_interval)
            self.event.wait(self.sleep_interval)
            self.event.clear()

    def shutdown(self):
        self.log.error('Stopping standby')
        if self.trap_on_shutdown == True:
            timeofday = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
            trapstring = timeofday+" Ceph Manager SNMP Handler - Standby Stopping"
            errorIndication, errorStatus, errorIndex, varBinds = next(
                sendNotification(
                    SnmpEngine(),
                    CommunityData('public', mpModel=0),
                    UdpTransportTarget((self.trap_addr, int(self.trap_port))),
                    ContextData(),
                    'trap',
                    NotificationType(
                        #ObjectIdentity('SNMPHANDLER-MIB', 'clusterCheck')
			ObjectIdentity('1.3.6.1.4.1.50495.10.4.0')
                    ).addVarBinds(
                        (ObjectIdentity('SNMPHANDLER-MIB', 'fsId'), OctetString(self.get_fsid())),
                        (ObjectIdentity('SNMPHANDLER-MIB', 'statusDetail'), '3'),
                        (ObjectIdentity('SNMPHANDLER-MIB', 'statusMsg'), OctetString(trapstring))
                    )
                )
            )
        self.run = False
        self.event.set()


class Module(MgrModule):
    config = dict()
    ceph_health_mapping = {'HEALTH_OK': 0, 'HEALTH_WARN': 1, 'HEALTH_ERR': 2, 'HEALTH_UNKNOWN': 3}

    COMMANDS = [
        {
            "cmd": "snmp trap_send "
                   "name=ip,type=CephString,req=true",
            "desc": "Send a test trap to this address:port",
            "perm": "r"
        },
        {
            "cmd": "snmp trap_on "
                   "name=ip,type=CephString,req=true",
            "desc": "Turn on automatic trap to be sent at this address:port",
            "perm": "rw"
        },
        {
            "cmd": "snmp trap_off ",
            "desc": "Turn off automatic trap",
            "perm": "rw"
        },
        {
            "cmd": "snmp listener_on "
                   "name=ip,type=CephString,req=true",
            "desc": "Turn on listening for SNMP get requests on this address:port",
            "perm": "rw"
        },
        {
            "cmd": "snmp listener_off ",
            "desc": "Turn off listening for SNMP get requests",
            "perm": "rw"
        }
    ]
    MODULE_OPTIONS = [
        {
            "name": "trap_addr"
        },
        {
            "name": "trap_port"
        },
        {
            "name": "trap_oid"
        },
        {
            "name": "sleep_interval"
        },
        {
            "name": "trap_on_start"
        },
        {
            "name": "trap_on_shutdown"
        }
    ]
    mon_connection = False
    run = True
    trap_addr = 'localhost'
    trap_port = '162'
    trap_oid = '1.3.6.1.4.1.50495.1.1.0'
    trap_on_start = False
    trap_on_shutdown = False
    sleep_interval = 30
    fsId = 'N/A'
    clusterHealth = None

    def __init__(self, *args, **kwargs):
        super(Module, self).__init__(*args, **kwargs)
        _global_instance['plugin'] = self
        self.log.error("Initialized parameters Dest={0}:{1}".format(self.trap_addr, self.trap_port))

        self.log.info("Constructing module {0}: instance {1}".format(
            __name__, _global_instance))
        self.event = Event()

        # Keep a librados instance for those that need it.
#        self._rados = None

        # A short history of pool df stats
        self.pool_stats = defaultdict(lambda: defaultdict(
            lambda: collections.deque(maxlen=10)))

#    @property
#    def rados(self):
#        """
#        A librados instance to be shared by any classes within
#        this mgr module that want one.
#        """
#        if self._rados:
#            return self._rados
#
#        ctx_capsule = self.get_context()
#        self._rados = rados.Rados(context=ctx_capsule)
#        self._rados.connect()
#
#        return self._rados

    def update_pool_stats(self):
        df = global_instance().get("df")
        pool_stats = dict([(p['id'], p['stats']) for p in df['pools']])
        now = time.time()
        for pool_id, stats in pool_stats.items():
            for stat_name, stat_val in stats.items():
                self.pool_stats[pool_id][stat_name].appendleft((now, stat_val))

    def get_fsid(self):
        if self.fsId == 'N/A':
           self.log.error("Cluster FSID has not been retrieved yet.")
           self.fsId = self.get('mon_map')['fsid']
           self.log.error("Cluster FSID discovered as {0}".format(self.fsId))
        return self.fsId

    def get_sync_object(self, object_type):
        if object_type == OsdMap:
            data = self.get("osd_map")

            assert data is not None

            data['tree'] = self.get("osd_map_tree")
            data['crush'] = self.get("osd_map_crush")
            data['crush_map_text'] = self.get("osd_map_crush_map_text")
            data['osd_metadata'] = self.get("osd_metadata")
            obj = OsdMap(data)
        elif object_type == Config:
            data = self.get("config")
            obj = Config( data)
        elif object_type == MonMap:
            data = self.get("mon_map")
            obj = MonMap(data)
        elif object_type == FsMap:
            data = self.get("fs_map")
            obj = FsMap(data)
        elif object_type == PgSummary:
            data = self.get("pg_summary")
            #self.log.debug("JSON: {0}".format(data))
            obj = PgSummary(data)
        elif object_type == Health:
            data = self.get("health")
            obj = Health(json.loads(data['json']))
        elif object_type == MonStatus:
            data = self.get("mon_status")
            obj = MonStatus(json.loads(data['json']))
        elif object_type == ServiceMap:
            data = self.get("service_map")
            obj = ServiceMap(data)
        else:
            raise NotImplementedError(object_type)

        return obj

    def send_unknown_trap(self):
        oidToUse = '1.3.6.1.4.1.50495.10.4.0'
        zeDetail = self.ceph_health_mapping['HEALTH_UNKNOWN']
        self.log.error("Lost contact with Monitors. Preparing notification for {0}".format('HEALTH_UNKNOWN'))

        timeofday = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
        trapstring = timeofday+" Ceph Manager SNMP Handler - Lost connection to Monitors"
        errorIndication, errorStatus, errorIndex, varBinds = next(
            sendNotification(
                SnmpEngine(),
                CommunityData('public', mpModel=0),
                UdpTransportTarget((self.trap_addr, int(self.trap_port))),
                ContextData(),
                'trap',
                NotificationType(
                    ObjectIdentity(oidToUse)
                ).addVarBinds(
                    (ObjectIdentity('SNMPHANDLER-MIB', 'fsId'), OctetString(self.get_fsid())),
                    (ObjectIdentity('SNMPHANDLER-MIB', 'statusDetail'), zeDetail),
                    (ObjectIdentity('SNMPHANDLER-MIB', 'statusMsg'), OctetString(trapstring))
                    )
                )
            )

        return self

    def send_health_trap(self, statusDetail):
        oidToUse = ''
        zeDetail = self.ceph_health_mapping['HEALTH_UNKNOWN']
        self.log.error("Preparing notification for {0}".format(statusDetail))
        if statusDetail == 'HEALTH_OK':
            oidToUse = '1.3.6.1.4.1.50495.10.1.0'
            zeDetail = self.ceph_health_mapping['HEALTH_OK']
        elif statusDetail == 'HEALTH_WARN':
            oidToUse = '1.3.6.1.4.1.50495.10.2.0'
            zeDetail = self.ceph_health_mapping['HEALTH_WARN']
        elif statusDetail == 'HEALTH_ERR':
            oidToUse = '1.3.6.1.4.1.50495.10.3.0'
            zeDetail = self.ceph_health_mapping['HEALTH_ERR']
        else:
            oidToUse = '1.3.6.1.4.1.50495.10.4.0'

        timeofday = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
        #trapstring = timeofday+" Ceph Manager SNMP Handler - Status Changed"
        trapstring = "Ceph Manager SNMP Handler - Status Changed"
        errorIndication, errorStatus, errorIndex, varBinds = next(
            sendNotification(
                SnmpEngine(),
                CommunityData('public', mpModel=0),
                UdpTransportTarget((self.trap_addr, int(self.trap_port))),
                ContextData(),
                'trap',
                NotificationType(
                    ObjectIdentity(oidToUse)
                ).addVarBinds(
                    (ObjectIdentity('SNMPHANDLER-MIB', 'fsId'), OctetString(self.get_fsid())),
                    (ObjectIdentity('SNMPHANDLER-MIB', 'statusDetail'), zeDetail),
                    (ObjectIdentity('SNMPHANDLER-MIB', 'statusMsg'), OctetString(trapstring))
                    )
                )
            )

        return self

    def process_health(self):
        firstRun = False
        health = global_instance().get_sync_object(Health).data
        # Transform the `checks` dict into a list for the convenience
        # of rendering from javascript.
        checks = []
        for k, v in health['checks'].iteritems():
            v['type'] = k
            checks.append(v)

        checks = sorted(checks, cmp=lambda a, b: a['severity'] > b['severity'])

        health['checks'] = checks
        if self.clusterHealth == None:
            self.clusterHealth = health
            firstRun = True

        if firstRun == True:
            self.log.error("Cluster status discovered {0}".format(health['status']))
            self.send_health_trap(health['overall_status'])
        else:
            if health['status'] != self.clusterHealth['status']: 
                self.log.error("Cluster status CHANGED {0}/{1}".format(health['status'], self.clusterHealth['status']))
                self.send_health_trap(health['status'])
                self.clusterHealth = health
            else:
                self.log.error("Cluster status REMAINED {0}/{1}".format(health['status'], self.clusterHealth['status']))
                self.clusterHealth = health

        return health

    def process_osdmap(self):
        osd_map = global_instance().get_sync_object(OsdMap).data
        self.log.debug(str(osd_map))
        return osd_map

    def process_monmap(self):
        mon_map = global_instance().get_sync_object(MonMap).data
        self.log.debug(str(mon_map))
        return mon_map

    def process_fsmap(self):
        fs_map = global_instance().get_sync_object(FsMap).data
        self.log.debug(str(fs_map))
        return fs_map

    def process_monstatus(self):
        mon_status = global_instance().get_sync_object(MonStatus).data
        self.log.debug(str(mon_status))
        return mon_status

    def process_svcmap(self):
        svc_map = global_instance().get_sync_object(ServiceMap).data
        self.log.debug(str(svc_map))
        return svc_map

    def notify(self, notify_type, notify_val):
        if notify_type == "pg_summary":
            #self.log.debug('Received notification : PG_SUMMARY')
            #self.update_pool_stats()
            pass
        elif notify_type == "osd_map":
            ze_osdmap = self.process_osdmap()
        elif notify_type == "mon_map":
            ze_monmap = self.process_monmap()
        elif notify_type == "fs_map":
            ze_fsmap = self.process_fsmap()
        elif notify_type == "mon_status":
            ze_monstatus = self.process_monstatus()
        elif notify_type == "health":
            ze_health = self.process_health()
        elif notify_type == "command":
            pass
        elif notify_type == "service_map":
            ze_svcmap = self.process_svcmap()
        else:
            pass

     
    def handle_trap_send(self, address):
        self.log.error('Destination='+str(address['ip']))
        parms = address['ip'].split(':', 1)
        dest_addr = parms[0]
        dest_port = parms[1]
        errorIndication, errorStatus, errorIndex, varBinds = next(
            sendNotification(
                SnmpEngine(),
                CommunityData('public', mpModel=0),
                UdpTransportTarget((dest_addr, int(dest_port))),
                ContextData(),
                'trap',
                NotificationType(ObjectIdentity('iso.3.6.1.2.1.1.1.0')).addVarBinds(('iso.3.6.1.2.1.1.1', OctetString('Ceph Manager SNMP Handler - Test Trap'))
                )
            )
        )

        if errorIndication:
            print(errorIndication)

        return 0, "", "Completed trap send command to " + str(dest_addr) + ":" + str(dest_port) + ".\n"

    def handle_trap_on(self, address):
        self.log.error('Destination='+str(address['ip']))
        parms = address['ip'].split(':', 1)
        self.trap_addr = parms[0]
        self.trap_port = parms[1]
        self.run = True
        return 0, "", "Completed trap ON command to " + str(self.trap_addr) + ":" + str(self.trap_port) + ".\n"

    def handle_trap_off(self):
        self.run = False
        return 0, "", "Completed trap OFF command.\n"

    def handle_listener_on(self, address):
        self.log.error('Listener='+str(address['ip']))

        return 0, "", "Completed listener on command at " + str(address) + ".\n"

    def handle_listener_off(self):

        return 0, "", "Completed listener off command.\n"

    def handle_command(self, cmd):
        self.log.error("Handling command: '%s'" % str(cmd))

        if cmd['prefix'] == "snmp trap_send":
            return self.handle_trap_send(cmd)
        elif cmd['prefix'] == "snmp trap_on":
            return self.handle_trap_on(cmd)
        elif cmd['prefix'] == "snmp trap_off":
            return self.handle_trap_off()
        elif cmd['prefix'] == "snmp listener_on":
            return self.handle_listener_on(cmd)
        elif cmd['prefix'] == "snmp listener_off":
            return self.handle_listener_off()
        else:
            return (-errno.EINVAL, '',
                    "Command not found '{0}'".format(cmd['prefix']))
#        else:
            # mgr should respect our self.COMMANDS and not call us for
            # any prefix we don't advertise
#            raise NotImplementedError(cmd['prefix'])
            

    def shutdown(self):
        self.log.error('Stopping active')
        if self.trap_on_shutdown == True:
            timeofday = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
            trapstring = timeofday+" Ceph Manager SNMP Handler - Active Stopping"
            errorIndication, errorStatus, errorIndex, varBinds = next(
                sendNotification(
                    SnmpEngine(),
                    CommunityData('public', mpModel=0),
                    UdpTransportTarget((self.trap_addr, int(self.trap_port))),
                    ContextData(),
                    'trap',
                    NotificationType(
                        #ObjectIdentity('SNMPHANDLER-MIB', 'clusterCheck')
			ObjectIdentity('1.3.6.1.4.1.50495.10.4.0')
                    ).addVarBinds(
                        (ObjectIdentity('SNMPHANDLER-MIB', 'fsId'), OctetString(self.get_fsid())),
                        (ObjectIdentity('SNMPHANDLER-MIB', 'statusDetail'), '3'),
                        (ObjectIdentity('SNMPHANDLER-MIB', 'statusMsg'), OctetString(trapstring))
                    )
                )
            )
        self.run = False
        self.event.set()

    def serve(self):
        module = self
        self.log.error('Starting active')
        #
        # Do not place calls to get_localized_conf in your __init__ class method
        # Doing so will trigger a failure causing the MGR to crash
        #
        self.trap_addr = self.get_localized_config('trap_addr', 'localhost')
        self.trap_port = self.get_localized_config('trap_port', '162')
        self.trap_oid = self.get_localized_config('trap_oid', '1.3.6.1.4.1.50495.99.1')
        self.sleep_interval = int(self.get_localized_config('sleep_interval', '30'))
        self.trap_on_start = int(self.get_localized_config('trap_on_start', '0'))
        self.trap_on_shutdown = int(self.get_localized_config('trap_on_shutdown', '0'))
        self.log.error("Active loaded parameters Dest={0} Port={1}".format(self.trap_addr, self.trap_port))
        self.log.error("                         OID ={0} Wait={1}".format(self.trap_oid, self.sleep_interval))
        if self.trap_on_start == True:
            timeofday = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
            trapstring = timeofday+" Ceph Manager SNMP Handler - Active Starting"
            errorIndication, errorStatus, errorIndex, varBinds = next(
                sendNotification(
                    SnmpEngine(),
                    CommunityData('public', mpModel=0),
                    UdpTransportTarget((self.trap_addr, int(self.trap_port))),
                    ContextData(),
                    'trap',
                    NotificationType(
                        #ObjectIdentity('SNMPHANDLER-MIB', 'clusterCheck')
			ObjectIdentity('1.3.6.1.4.1.50495.10.4.0')
                    ).addVarBinds(
                        (ObjectIdentity('SNMPHANDLER-MIB', 'fsId'), OctetString(self.get_fsid())),
                        (ObjectIdentity('SNMPHANDLER-MIB', 'statusDetail'), '3'),
                        (ObjectIdentity('SNMPHANDLER-MIB', 'statusMsg'), OctetString(trapstring))
                    )
                )
            )
        while self.run:
            if (self.have_mon_connection()):
                self.mon_connection = True
                servers = self.list_servers()
                for server in servers:
                    self.get_server(server['hostname'])
            else:
                self.mon_connection = False
                self.send_unknown_trap()
            self.log.error('Ceph Mon Connection is %d', self.mon_connection)
            timeofday = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
            trapstring = timeofday+" Ceph Manager SNMP Handler - Wake up time"
            self.log.error('Sleeping for %d', self.sleep_interval)
            self.event.wait(self.sleep_interval)
            #self.log.error("Sending trap to '%s'" % self.trap_addr+' '+self.trap_port)
            #errorIndication, errorStatus, errorIndex, varBinds = next(
            #    sendNotification(
            #        SnmpEngine(),
            #        CommunityData('public', mpModel=0),
            #        UdpTransportTarget((self.trap_addr, int(self.trap_port))),
            #        ContextData(),
            #        'trap',
            #        NotificationType(
			#ObjectIdentity('SNMPHANDLER-MIB', 'clusterCheck')
            #            ObjectIdentity('1.3.6.1.4.1.50495.10.4.0')
            #        ).addVarBinds(
            #            (ObjectIdentity('SNMPHANDLER-MIB', 'fsId'), OctetString(self.get_fsid())),
            #            (ObjectIdentity('SNMPHANDLER-MIB', 'statusDetail'), Integer('3')),
            #            (ObjectIdentity('SNMPHANDLER-MIB', 'statusMsg'), OctetString(trapstring))
            #        )
            #    )
            #)

            if errorIndication:
                print(errorIndication)
            self.event.clear()            

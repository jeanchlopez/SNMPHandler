"""
Set up an SNMP Module on top of the Ceph Manager
This module is responsible for monitoring the che changes in the cluster 
status and to issue an SNMP trap to a specified destination:port.

All parameters can be tuned (see MODULE_OPTIONS)
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
import socket
import commands

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
    #
    # Mapping of the cluster status to convert to for the traps
    #
    ceph_health_mapping = {'HEALTH_OK': 0, 'HEALTH_WARN': 1, 'HEALTH_ERR': 2, 'HEALTH_UNKNOWN': 3}
    ceph_trap_mapping = {0: 'clusterOk', 1: 'clusterWarn', 2: 'clusterError', 3: 'clusterCheck'}
    #
    # Run tim variable to track if we lost connection with the Monitors (partition is not quorate)
    #
    mon_connection = False
    run = True
    # 
    # Where do we send the trap.
    # Configurable using Ceph Manager option config-key snmphandler/trap_addr
    #
    trap_addr = 'localhost'
    # 
    # What port do we send the trap.
    # Configurable using Ceph Manager option config-key snmphandler/trap_port
    #
    trap_port = '162'
    # 
    # What OID base do we we use for the trap.
    # Configurable using Ceph Manager option config-key snmphandler/trap_oid
    #
    trap_oid = '1.3.6.1.4.1.50495'
    # 
    # Do we send a test trap when the module starts.
    # Configurable using Ceph Manager option config-key snmphandler/trap_on_start
    #
    trap_on_start = False
    # 
    # Do we send a test trap when the module stops.
    # Configurable using Ceph Manager option config-key snmphandler/trap_on_stop
    #
    trap_on_shutdown = False
    # 
    # Use the local net-snmp-utils snmptrap command to send the trap.
    # Configurable using Ceph Manager option config-key snmphandler/trap_using_tools
    #
    trap_using_tools = True
    #
    # Run time variable to set the sleep time in the module loop.
    # Configurable using Ceph Manager option config-key snmphandler/sleep_interval
    #
    sleep_interval = 30
    #
    # Run time variable to store the cluster fsId we are connected to
    #
    fsId = 'N/A'
    #
    # Run time variable to track the status of the cluster
    #
    clusterHealth = None
    #
    # SNMP protovol variables
    #
    snmp_version = '1'
    snmp_community = 'public'
    snmpv3_engine = '0x8000000001020304'
    snmpv3_user = 'ceph'
    snmpv3_auth = 'SHA:mypassword'
    snmpv3_enc = 'AES:mypassword'
    snmpv3_level = 'noAuthNoPriv'
    #
    # Available options are noAuthNoPriv|authNoPriv|authPriv
    #

    def __init__(self, *args, **kwargs):
        super(StandbyModule, self).__init__(*args, **kwargs)
        _global_instance['plugin'] = self
        self.log.debug("Initialized parameters Dest={0}:{1}".format(self.trap_addr, self.trap_port))

        self.log.debug("Constructing module {0}: instance {1}".format(
            __name__, _global_instance))
        self.event = Event()

    def serve(self):
        module = self
        self.trap_addr = self.get_localized_config('trap_addr', 'localhost')
        self.trap_port = self.get_localized_config('trap_port', '162')
        self.trap_oid = self.get_localized_config('trap_oid', '1.3.6.1.4.1.50495')
        self.sleep_interval = int(self.get_localized_config('sleep_interval', '30'))
        self.trap_on_start = int(self.get_localized_config('trap_on_start', '0'))
        self.trap_on_shutdown = int(self.get_localized_config('trap_on_shutdown', '0'))
        self.trap_using_tools = self.get_localized_config('trap_using_tools', True)
        #
        # SNMP V1/V2C Parameters
        #
        self.snmp_version = self.get_localized_config('snmp_version', '1')
        self.snmp_community = self.get_localized_config('snmp_community', 'public')
        #
        # SNMP V3 Parameters
        #
        self.snmpv3_engine = self.get_localized_config('snmpv3_engine', '0x8000000001020304')
        self.snmpv3_user = self.get_localized_config('snmpv3_user', 'ceph')
        self.snmpv3_pass = self.get_localized_config('snmpv3_pass', 'SHA:cephpassword')
        self.snmpv3_enc = self.get_localized_config('snmpv3_enc', 'AES:cephpassword')
        self.snmpv3_level = self.get_localized_config('snmpv3_level', 'noAuthNoPriv')
        #
        self.log.error("Standby loaded parameters Destination = {0}".format(self.trap_addr))
        self.log.error("                          Port        = {0}".format(self.trap_port))
        self.log.error("                          OID         = {0}".format(self.trap_oid))
        self.log.error("                          Wait        = {0}".format(self.sleep_interval))
        self.log.error("                          SNMP Version= {0}".format(self.snmp_version))
        self.log.error("                          Community   = {0}".format(self.snmp_community))
        self.log.error("                          Engine      = {0}".format(self.snmpv3_engine))
        self.log.error("                          User        = {0}".format(self.snmpv3_user))
        self.log.error("                          Pass        = {0}".format(self.snmpv3_pass))
        self.log.error("                          Enc         = {0}".format(self.snmpv3_enc))
        self.log.error("                          Security    = {0}".format(self.snmpv3_level))
        self.log.error("                          Use Tools   = {0}".format(self.trap_using_tools))
        self.log.info('Starting standby')
        if self.trap_on_start == True:
            timeofday = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
            trapstring = timeofday+" Ceph Manager SNMP Handler - Standby Starting"
            self.send_generic_trap(self.ceph_health_mapping['HEALTH_UNKNOWN'], trapstring)

        while self.run:
            timeofday = time.strftime('%H%M', time.localtime())
            self.log.info('Sleeping for %d', self.sleep_interval)
            self.event.wait(self.sleep_interval)
            self.event.clear()

    def shutdown(self):
        self.log.info('Stopping standby')
        if self.trap_on_shutdown == True:
            timeofday = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
            trapstring = timeofday+" Ceph Manager SNMP Handler - Standby Stopping"
            self.send_generic_trap(self.ceph_health_mapping['HEALTH_UNKNOWN'], trapstring)

        self.run = False
        self.event.set()

    def get_fsid(self):
        if self.fsId == 'N/A':
           self.log.info("Cluster FSID has not been retrieved yet.")
        return self.fsId

    def send_unknown_trap(self):
        zeDetail = self.ceph_health_mapping['HEALTH_UNKNOWN']
        self.log.info("Lost contact with Monitors. Preparing notification for {0}".format('HEALTH_UNKNOWN'))

        timeofday = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
        trapstring = timeofday+" Ceph Manager SNMP Handler - Lost connection to Monitors"
        self.send_generic_trap(zeDetail, trapstring)

        return self
    #
    # A single way to send traps according to the arguments passed
    # Based on the value of statusDetail
    # - 0 : Send a clusterOk trap
    # - 1 : Send a clusterWarn trap
    # - 2 : Send a clusterError trap
    # - 3 : Send a clusterCheck trap
    #
    def send_generic_trap(self, statusDetail, statusMsg):
        self.log.debug("SNMP Version --> "+str(self.snmp_version))
        self.log.debug("statusDetail --> "+str(statusDetail))
        self.log.debug("statusMsg    --> "+str(statusMsg))
        if self.trap_using_tools == True:
           if self.snmp_version == '1':
              commandLine = 'snmptrap -m +SNMPHANDLER-MIB -v '+str(self.snmp_version)+' -c '+self.snmp_community+' '+self.trap_addr+':'+str(self.trap_port)+' '+self.ceph_trap_mapping[statusDetail]+' '+socket.gethostname()+' 6 0 0 fsId s '+self.get_fsid()+' statusDetail i '+str(statusDetail)+' statusMsg s "'+statusMsg+'"'
           elif self.snmp_version == '2c':
              commandLine = 'snmptrap -m +SNMPHANDLER-MIB -v '+str(self.snmp_version)+' -c '+self.snmp_community+' '+self.trap_addr+':'+str(self.trap_port)+' 0 '+self.ceph_trap_mapping[statusDetail]+' fsId s '+self.get_fsid()+' statusDetail i '+str(statusDetail)+' statusMsg s "'+statusMsg+'"'
           elif self.snmp_version == '3':
              if self.snmpv3_level == 'noAuthNoPriv':
                 commandLine = 'snmptrap -m +SNMPHANDLER-MIB -v '+str(self.snmp_version)+' -u '+self.snmpv3_user+' -l noAuthNoPriv -e '+self.snmpv3_engine+' '+self.trap_addr+':'+str(self.trap_port)+' 0 '+self.ceph_trap_mapping[statusDetail]+' fsId s '+self.get_fsid()+' statusDetail i '+str(statusDetail)+' statusMsg s "'+statusMsg+'"'
              elif self.snmpv3_level == 'authNoPriv':
                 user_parms = self.snmpv3_pass.split(':', 1)
                 commandLine = 'snmptrap -m +SNMPHANDLER-MIB -v '+str(self.snmp_version)+' -u '+self.snmpv3_user+' -a '+user_parms[0]+' -A '+user_parms[1]+' -l authNoPriv -e '+self.snmpv3_engine+' '+self.trap_addr+':'+str(self.trap_port)+' 0 '+self.ceph_trap_mapping[statusDetail]+' fsId s '+self.get_fsid()+' statusDetail i '+str(statusDetail)+' statusMsg s "'+statusMsg+'"'
              elif self.snmpv3_level == 'authPriv':
                 user_parms = self.snmpv3_pass.split(':', 1)
                 enc_parms = self.snmpv3_enc.split(':', 1)
                 commandLine = 'snmptrap -m +SNMPHANDLER-MIB -v '+str(self.snmp_version)+' -u '+self.snmpv3_user+' -a '+user_parms[0]+' -A '+user_parms[1]+' -x '+enc_parms[0]+' -X '+enc_parms[1]+' -l authPriv -e '+self.snmpv3_engine+' '+self.trap_addr+':'+str(self.trap_port)+' 0 '+self.ceph_trap_mapping[statusDetail]+' fsId s '+self.get_fsid()+' statusDetail i '+str(statusDetail)+' statusMsg s "'+statusMsg+'"'
              else:
                 self.log.error("Invalid security level string: "+self.snmpv3_level)
           else:
              self.log.error("SNMP Version not supported --> "+str(self.snmp_version))
              return self

           self.log.debug("--> "+commandLine)
           (code, raw) = commands.getstatusoutput(commandLine)
           if code != 0:
              self.log.error("--> "+commandLine)
              self.log.error("--> Failed to send trap. RC={0}".format(code))
        else:
           self.log.error("We only support sending traps using local tools for now")

        return self


class Module(MgrModule):
    config = dict()
    #
    # Mapping of the cluster status to convert to for the traps
    #
    ceph_health_mapping = {'HEALTH_OK': 0, 'HEALTH_WARN': 1, 'HEALTH_ERR': 2, 'HEALTH_UNKNOWN': 3}
    ceph_trap_mapping = {0: 'clusterOk', 1: 'clusterWarn', 2: 'clusterError', 3: 'clusterCheck'}
    #
    # Run tim variable to track if we lost connection with the Monitors (partition is not quorate)
    #
    mon_connection = False
    #
    # Do we activate the server loop at run time
    #
    run = True
    # 
    # Where do we send the trap.
    # Configurable using Ceph Manager option config-key snmphandler/trap_addr
    #
    trap_addr = 'localhost'
    # 
    # What port do we send the trap.
    # Configurable using Ceph Manager option config-key snmphandler/trap_port
    #
    trap_port = '162'
    # 
    # What OID base do we we use for the trap.
    # Configurable using Ceph Manager option config-key snmphandler/trap_oid
    #
    trap_oid = '1.3.6.1.4.1.50495.1.1.0'
    # 
    # Do we send a test trap when the module starts.
    # Configurable using Ceph Manager option config-key snmphandler/trap_on_start
    #
    trap_on_start = False
    # 
    # Do we send a test trap when the module stops.
    # Configurable using Ceph Manager option config-key snmphandler/trap_on_stop
    #
    trap_on_shutdown = False
    # 
    # Use the local net-snmp-utils snmptrap command to send the trap.
    # Configurable using Ceph Manager option config-key snmphandler/trap_using_tools
    #
    trap_using_tools = True
    #
    # Run time variable to set the sleep time in the module loop.
    # Configurable using Ceph Manager option config-key snmphandler/sleep_interval
    #
    sleep_interval = 30
    #
    # Run time variable to store the cluster fsId we are connected to
    #
    fsId = 'N/A'
    #
    # Run time variable to track the status of the cluster
    #
    clusterHealth = None
    #
    # SNMP protovol variables
    #
    snmp_version = '1'
    snmp_community = 'public'
    snmpv3_engine = '0x8000000001020304'
    snmpv3_user = 'ceph'
    snmpv3_auth = 'SHA:mypassword'
    snmpv3_enc = 'AES:mypassword'
    snmpv3_level = 'noAuthNoPriv'
    #
    # Available options are noAuthNoPriv|authNoPriv|authPriv
    #

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
        { # To which address we trap to: default is localhost
            "name": "trap_addr"
        },
        { # To which port we trap to: default is 162
            "name": "trap_port"
        },
        { # Base OID to send as the trap OID: RFU
            "name": "trap_oid"
        },
        { # Sleep time in the server loop: RFU default is 30
            "name": "sleep_interval"
        },
        { # Send a clusterCheck trap on startup default is False
            "name": "trap_on_start"
        },
        { # Send a clusterCheck trap on shutdown default is False
            "name": "trap_on_shutdown"
        },
        { # Send SNMP traps using the local command line default is True
            "name": "trap_using_tools"
        },
        { # The SNMP version to use: 1 | 2c | 3 default is 1
            "name": "snmp_version"
        },
        { # The SNMP community name to use for SNMP v1 and v2c default is public
            "name": "snmp_community"
        },
        { # The SNMP v3 engine ID to use: default is 0x8000000001020304
            "name": "snmpv3_engine"
        },
        { # The SNMP v3 user name to use: default is cepg
            "name": "snmpv3_user"
        },
        { # The SNMP v3 authentication pass phrase to use: default is SHA:cephpassword
            "name": "snmpv3_pass"
        },
        { # The SNMP v3 encyrption pass phrase to use: default is AES:cephpassword
            "name": "snmpv3_enc"
        },
        { # The SNMP v3 security setting: authPriv, authNoPriv, noAuthPriv, noAuthNoPriv default is noAuthNoPriv
            "name": "snmpv3_level"
        }
    ]

    def __init__(self, *args, **kwargs):
        super(Module, self).__init__(*args, **kwargs)
        _global_instance['plugin'] = self
        self.log.debug("Initialized parameters Dest={0}:{1}".format(self.trap_addr, self.trap_port))

        self.log.debug("Constructing module {0}: instance {1}".format(
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
           self.log.info("Cluster FSID has not been retrieved yet.")
           self.fsId = self.get('mon_map')['fsid']
           self.log.info("Cluster FSID discovered as {0}".format(self.fsId))
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
    #
    # A function dedicated to sending a test trap to a custom
    # location but using the configured SNMP parameters
    # - SNMP Version
    # - SNMP EngineID
    # - SNMP Username
    # - SNMP Authentication password
    # - SNMP Encryption password
    #
    def send_test_trap(self, toHost, toPort):
        if self.trap_using_tools == True:
           if self.snmp_version == '1':
              commandLine = 'snmptrap -m +SNMPHANDLER-MIB -v '+str(self.snmp_version)+' -c '+self.snmp_community+' '+toHost+':'+str(toPort)+' clusterCheck '+socket.gethostname()+' 6 0 0 fsId s '+self.get_fsid()+' statusDetail i '+str(self.ceph_health_mapping['HEALTH_OK'])+' statusMsg s "Ceph Manager SNMP Handler - Test Trap SNMP v1"'
           elif self.snmp_version == '2c':
              commandLine = 'snmptrap -m +SNMPHANDLER-MIB -v '+str(self.snmp_version)+' -c '+self.snmp_community+' '+toHost+':'+str(toPort)+' 0 clusterCheck fsId s '+self.get_fsid()+' statusDetail i '+str(self.ceph_health_mapping['HEALTH_OK'])+' statusMsg s "Ceph Manager SNMP Handler - Test Trap SNMP v2c"'
           elif self.snmp_version == '3':
              if self.snmpv3_level == 'noAuthNoPriv':
                 commandLine = 'snmptrap -m +SNMPHANDLER-MIB -v '+str(self.snmp_version)+' -u '+self.snmpv3_user+' -l noAuthNoPriv -e '+self.snmpv3_engine+' '+toHost+':'+str(toPort)+' 0 clusterCheck fsId s '+self.get_fsid()+' statusDetail i '+str(self.ceph_health_mapping['HEALTH_OK'])+' statusMsg s "Ceph Manager SNMP Handler - Test Trap SNMP v3 noAuthNoPriv"'
              elif self.snmpv3_level == 'authNoPriv':
                 user_parms = self.snmpv3_pass.split(':', 1)
                 commandLine = 'snmptrap -m +SNMPHANDLER-MIB -v '+str(self.snmp_version)+' -u '+self.snmpv3_user+' -a '+user_parms[0]+' -A '+user_parms[1]+' -l authNoPriv -e '+self.snmpv3_engine+' '+toHost+':'+str(toPort)+' 0 clusterCheck fsId s '+self.get_fsid()+' statusDetail i '+str(self.ceph_health_mapping['HEALTH_OK'])+' statusMsg s "Ceph Manager SNMP Handler - Test Trap SNMP v3 authNoPriv"'
              elif self.snmpv3_level == 'authPriv':
                 user_parms = self.snmpv3_pass.split(':', 1)
                 enc_parms = self.snmpv3_enc.split(':', 1)
                 commandLine = 'snmptrap -m +SNMPHANDLER-MIB -v '+str(self.snmp_version)+' -u '+self.snmpv3_user+' -a '+user_parms[0]+' -A '+user_parms[1]+' -x '+enc_parms[0]+' -X '+enc_parms[1]+' -l authPriv -e '+self.snmpv3_engine+' '+toHost+':'+str(toPort)+' 0 clusterCheck fsId s '+self.get_fsid()+' statusDetail i '+str(self.ceph_health_mapping['HEALTH_OK'])+' statusMsg s "Ceph Manager SNMP Handler - Test Trap SNMP v3 authPriv"'
              else:
                 self.log.error("Invalid security level string: "+self.snmpv3_level)
           else:
              self.log.error("SNMP Version not supported --> "+str(self.snmp_version))
              return self

           self.log.debug("--> "+commandLine)
           (code, raw) = commands.getstatusoutput(commandLine)
           if code != 0:
              self.log.error("--> "+commandLine)
              self.log.error("--> Failed to send trap. RC={0}".format(code))
        else:
           self.log.error("We only support sending traps using local tools for now")
        
        return self

    def send_check_trap(self, statusDetail, statusMsg):
        if self.trap_using_tools == True:
           if self.snmp_version == '1':
              commandLine = 'snmptrap -m +SNMPHANDLER-MIB -v '+str(self.snmp_version)+' -c '+self.snmp_community+' '+self.trap_addr+':'+str(self.trap_port)+' clusterCheck '+socket.gethostname()+' 6 0 0 fsId s '+self.get_fsid()+' statusDetail i '+str(statusDetail)+' statusMsg s "'+statusMsg+'"'
           elif self.snmp_version == '2c':
              commandLine = 'snmptrap -m +SNMPHANDLER-MIB -v '+str(self.snmp_version)+' -c '+self.snmp_community+' '+self.trap_addr+':'+str(self.trap_port)+' 0 clusterCheck fsId s '+self.get_fsid()+' statusDetail i '+str(statusDetail)+' statusMsg s "'+statusMsg+'"'
           else:
              self.log.error("SNMP Version not supported --> "+str(self.snmp_version))
              return self


           self.log.debug("--> "+commandLine)
           (code, raw) = commands.getstatusoutput(commandLine)
           self.log.debug("--> RC {0}".format(code))
        else:
           self.log.error("We only support sending traps using local tools for now")
        
        return self
    #
    # A single way to send traps according to the arguments passed
    # Based on the value of statusDetail
    # - 0 : Send a clusterOk trap
    # - 1 : Send a clusterWarn trap
    # - 2 : Send a clusterError trap
    # - 3 : Send a clusterCheck trap
    #
    def send_generic_trap(self, statusDetail, statusMsg):
        self.log.debug("SNMP Version --> "+str(self.snmp_version))
        self.log.debug("statusDetail --> "+str(statusDetail))
        self.log.debug("statusMsg    --> "+str(statusMsg))
        if self.trap_using_tools == True:
           if self.snmp_version == '1':
              commandLine = 'snmptrap -m +SNMPHANDLER-MIB -v '+str(self.snmp_version)+' -c '+self.snmp_community+' '+self.trap_addr+':'+str(self.trap_port)+' '+self.ceph_trap_mapping[statusDetail]+' '+socket.gethostname()+' 6 0 0 fsId s '+self.get_fsid()+' statusDetail i '+str(statusDetail)+' statusMsg s "'+statusMsg+'"'
           elif self.snmp_version == '2c':
              commandLine = 'snmptrap -m +SNMPHANDLER-MIB -v '+str(self.snmp_version)+' -c '+self.snmp_community+' '+self.trap_addr+':'+str(self.trap_port)+' 0 '+self.ceph_trap_mapping[statusDetail]+' fsId s '+self.get_fsid()+' statusDetail i '+str(statusDetail)+' statusMsg s "'+statusMsg+'"'
           elif self.snmp_version == '3':
              if self.snmpv3_level == 'noAuthNoPriv':
                 commandLine = 'snmptrap -m +SNMPHANDLER-MIB -v '+str(self.snmp_version)+' -u '+self.snmpv3_user+' -l noAuthNoPriv -e '+self.snmpv3_engine+' '+self.trap_addr+':'+str(self.trap_port)+' 0 '+self.ceph_trap_mapping[statusDetail]+' fsId s '+self.get_fsid()+' statusDetail i '+str(statusDetail)+' statusMsg s "'+statusMsg+'"'
              elif self.snmpv3_level == 'authNoPriv':
                 user_parms = self.snmpv3_pass.split(':', 1)
                 commandLine = 'snmptrap -m +SNMPHANDLER-MIB -v '+str(self.snmp_version)+' -u '+self.snmpv3_user+' -a '+user_parms[0]+' -A '+user_parms[1]+' -l authNoPriv -e '+self.snmpv3_engine+' '+self.trap_addr+':'+str(self.trap_port)+' 0 '+self.ceph_trap_mapping[statusDetail]+' fsId s '+self.get_fsid()+' statusDetail i '+str(statusDetail)+' statusMsg s "'+statusMsg+'"'
              elif self.snmpv3_level == 'authPriv':
                 user_parms = self.snmpv3_pass.split(':', 1)
                 enc_parms = self.snmpv3_enc.split(':', 1)
                 commandLine = 'snmptrap -m +SNMPHANDLER-MIB -v '+str(self.snmp_version)+' -u '+self.snmpv3_user+' -a '+user_parms[0]+' -A '+user_parms[1]+' -x '+enc_parms[0]+' -X '+enc_parms[1]+' -l authPriv -e '+self.snmpv3_engine+' '+self.trap_addr+':'+str(self.trap_port)+' 0 '+self.ceph_trap_mapping[statusDetail]+' fsId s '+self.get_fsid()+' statusDetail i '+str(statusDetail)+' statusMsg s "'+statusMsg+'"'
              else:
                 self.log.error("Invalid security level string: "+self.snmpv3_level)
           else:
              self.log.error("SNMP Version not supported --> "+str(self.snmp_version))
              return self

           self.log.debug("--> "+commandLine)
           (code, raw) = commands.getstatusoutput(commandLine)
           if code != 0:
              self.log.error("--> "+commandLine)
              self.log.error("--> Failed to send trap. RC={0}".format(code))
        else:
           self.log.error("We only support sending traps using local tools for now")

        return self
    #
    # A function dedicated to sending a clusterCheck - healthUnkown trap
    #
    def send_unknown_trap(self):
        zeDetail = self.ceph_health_mapping['HEALTH_UNKNOWN']
        self.log.info("Lost contact with Monitors. Preparing notification for {0}".format('HEALTH_UNKNOWN'))

        timeofday = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
        trapstring = timeofday+" Ceph Manager SNMP Handler - Lost connection to Monitors"
        self.send_generic_trap(zeDetail, trapstring)

        return self
    #
    # A function dedicated to sending traps based on cluster health
    #
    def send_health_trap(self, statusDetail):
        zeDetail = self.ceph_health_mapping['HEALTH_UNKNOWN']
        self.log.debug("Preparing notification for {0}".format(statusDetail))

        timeofday = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
        #trapstring = timeofday+" Ceph Manager SNMP Handler - Status Changed"
        trapstring = "Ceph Manager SNMP Handler - Status Changed"

        if statusDetail == 'HEALTH_OK':
            zeDetail = self.ceph_health_mapping['HEALTH_OK']
            self.send_generic_trap(zeDetail, trapstring)
        elif statusDetail == 'HEALTH_WARN':
            zeDetail = self.ceph_health_mapping['HEALTH_WARN']
            self.send_generic_trap(zeDetail, trapstring)
        elif statusDetail == 'HEALTH_ERR':
            zeDetail = self.ceph_health_mapping['HEALTH_ERR']
            self.send_generic_trap(zeDetail, trapstring)
        else:
            self.send_generic_trap(zeDetail, trapstring)

        return self
    #
    # A function dedicated to the tracking of the cluster status
    # and to detect changes correctly
    #
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
            self.log.debug("Cluster status discovered {0}".format(health['status']))
            self.send_health_trap(health['status'])
        else:
            if health['status'] != self.clusterHealth['status']: 
                self.log.debug("Cluster status CHANGED {0}/{1}".format(health['status'], self.clusterHealth['status']))
                self.send_health_trap(health['status'])
                self.clusterHealth = health
            else:
                self.log.debug("Cluster status REMAINED {0}/{1}".format(health['status'], self.clusterHealth['status']))
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
        self.log.info('Destination='+str(address['ip']))
        parms = address['ip'].split(':', 1)
        dest_addr = parms[0]
        dest_port = parms[1]
        self.send_test_trap(dest_addr, dest_port)

        return 0, "", "Completed trap send command to " + str(dest_addr) + ":" + str(dest_port) + ".\n"

    def handle_trap_on(self, address):
        self.log.info('Destination='+str(address['ip']))
        parms = address['ip'].split(':', 1)
        self.trap_addr = parms[0]
        self.trap_port = parms[1]
        self.run = True
        return 0, "", "Completed trap ON command to " + str(self.trap_addr) + ":" + str(self.trap_port) + ".\n"

    def handle_trap_off(self):
        self.run = False
        return 0, "", "Completed trap OFF command.\n"

    def handle_listener_on(self, address):
        self.log.info('Listener='+str(address['ip']))

        return 0, "", "Completed listener on command at " + str(address) + ".\n"

    def handle_listener_off(self):

        return 0, "", "Completed listener off command.\n"

    def handle_command(self, cmd):
        self.log.debug("Handling command: '%s'" % str(cmd))

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
            self.send_generic_trap(self.ceph_health_mapping['HEALTH_UNKNOWN'], trapstring)

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
        self.trap_oid = self.get_localized_config('trap_oid', '1.3.6.1.4.1.50495')
        self.sleep_interval = int(self.get_localized_config('sleep_interval', '30'))
        self.trap_on_start = int(self.get_localized_config('trap_on_start', '0'))
        self.trap_on_shutdown = int(self.get_localized_config('trap_on_shutdown', '0'))
        self.trap_using_tools = int(self.get_localized_config('trap_using_tools', True))
        #
        # SNMP V1/V2C Parameters
        #
        self.snmp_version = self.get_localized_config('snmp_version', '1')
        self.snmp_community = self.get_localized_config('snmp_community', 'public')
        #
        # SNMP V3 Parameters
        #
        self.snmpv3_engine = self.get_localized_config('snmpv3_engine', '0x8000000001020304')
        self.snmpv3_user = self.get_localized_config('snmpv3_user', 'ceph')
        self.snmpv3_pass = self.get_localized_config('snmpv3_pass', 'SHA:cephpassword')
        self.snmpv3_enc = self.get_localized_config('snmpv3_enc', 'AES:cephpassword')
        self.snmpv3_level = self.get_localized_config('snmpv3_level', 'noAuthNoPriv')
        #
        self.log.error("Active loaded parameters  Destination = {0}".format(self.trap_addr))
        self.log.error("                          Port        = {0}".format(self.trap_port))
        self.log.error("                          OID         = {0}".format(self.trap_oid))
        self.log.error("                          Wait        = {0}".format(self.sleep_interval))
        self.log.error("                          SNMP Version= {0}".format(self.snmp_version))
        self.log.error("                          Community   = {0}".format(self.snmp_community))
        self.log.error("                          Engine      = {0}".format(self.snmpv3_engine))
        self.log.error("                          User        = {0}".format(self.snmpv3_user))
        self.log.error("                          Pass        = {0}".format(self.snmpv3_pass))
        self.log.error("                          Enc         = {0}".format(self.snmpv3_enc))
        self.log.error("                          Security    = {0}".format(self.snmpv3_level))
        self.log.error("                          Use Tools   = {0}".format(self.trap_using_tools))

        if self.trap_on_start == True:
            timeofday = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
            trapstring = timeofday+" Ceph Manager SNMP Handler - Active Starting"
            self.send_generic_trap(self.ceph_health_mapping['HEALTH_UNKNOWN'], trapstring)

        while self.run:
            if (self.have_mon_connection()):
                self.mon_connection = True
                servers = self.list_servers()
                for server in servers:
                    self.get_server(server['hostname'])
                    self.log.error(str(server['hostname']))
            else:
                #
                # To make sure we recheck the status of the cluster when
                # the conmnection to the Monitors is restored to make sure
                # we issue a new trap on the next map update.
                #
                self.clusterHealth = None
                # 
                # And remember we lost the connection for future enhancements
                #
                self.mon_connection = False
                self.send_unknown_trap()
            self.log.info('Ceph Mon Connection is %d', self.mon_connection)
            timeofday = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
            trapstring = timeofday+" Ceph Manager SNMP Handler - Wake up time"
            self.log.debug('Sleeping for %d', self.sleep_interval)
            self.event.wait(self.sleep_interval)
            self.log.debug("Sending trap to '%s'" % self.trap_addr+':'+self.trap_port)
            #self.send_generic_trap(self.ceph_health_mapping['HEALTH_UNKNOWN'], trapstring)

            self.event.clear()            

# environment : Python3 (tested on Fedora)
# this code relies on two python packages
# - pysnmp in OSP EL8 beta channel as python3-pysnmp
#   --> should just be able to tag this to rhcs repo
# - pysmi in the rhgs cluster el7 channel
#   --> will need a new package for EL8 within the ceph tools repo

# TESTING
# place the MY-MIB.txt in /usr/share/snmp/mibs
# run this script as root/sudo (binding to 161 requires elevated privileges)

# What does this do?
# 1. Finds the MIB in it's normal hiding place
# 2. compiles it into pysnmp format - which validates it too
# 3. loads the compiled mib and associated the scalars with methods in a mib
#    handler class. mib handler holds a cephcluster class, and responds with
#    data from this cephcluster instance
# 4. optionally starts an SNMP listener that supports get/walk across the MIB
#    (MY-MIB.txt) NB. the listener runs in a separate thread
# 5. sends a trap (v1 or v2c only) with an optional varbind to a trap receiver
#    (running on localhost - I've just been using snmptrapd)

try:
    from pysnmp.entity import engine, config
    from pysnmp import debug
    from pysnmp.entity.rfc3413 import cmdrsp, context
    # from pysnmp.proto.api import v2c
    from pysnmp.carrier.asynsock.dgram import udp
    from pysnmp.carrier.error import CarrierError

    import pysnmp.hlapi as hlapi

    from pysnmp.smi import builder, view, rfc1902

    from pysmi.reader import FileReader
    from pysmi.searcher import PyFileSearcher, PyPackageSearcher, StubSearcher
    from pysmi.writer import PyFileWriter
    from pysmi.parser import SmiStarParser
    from pysmi.codegen import PySnmpCodeGen
    from pysmi.compiler import MibCompiler

except ImportError:
    pysnmp_imported = False
    pysmi_imported = False
else:
    pysnmp_imported = True
    pysmi_imported = True

import os
import sys
import time     # Just for testing
import threading

import logging

log = logging.getLogger(__name__)

# can be useful for in depth debugging. otherwise leave it off
# debug.setLogger(debug.Debug('all'))


class SNMPAgent(threading.Thread):
    daemon = True

    def __init__(self, mib_handler, listen_port=161, target='localhost'):
        super(SNMPAgent, self).__init__()

        self._engine = engine.SnmpEngine()
        self._context = context.SnmpContext(self._engine)
        self.healthy = True
        self.listen_port = listen_port
        self.target = target

        # object types are used to limit the name_to_oid dict
        self.valid_types = ['MibScalar', 'NotificationType']
        self.name_to_OID = {
            "MibScalar": dict(),
            "NotificationType": dict()
        }   # convert name to OID

        self._mib = mib_handler
        self._mibview = None
        self.load_mib()

    def send_trap(self, trap_name, var_binds=None):
        """send a trap
        :param trap_name: (str) name of the trap from the MIB
        :param var_binds: (list) list of additional vars to attach to the trap
                          each var corresponds to a scalar method in the mib handler
        """

        log.debug("sending trap - '{}'".format(trap_name))
        log.debug("attaching varbinds for: {}".format(','.join(var_binds)))
        binds = list(tuple())
        for var_name in var_binds:
            if var_name not in self.name_to_OID['MibScalar']:
                log.warning("var bind requested for a not implemented method in the mib handler")
                continue

            method = getattr(self._mib, var_name)
            mib_var = rfc1902.ObjectType(
                rfc1902.ObjectIdentity(self._mib._name, var_name, 0), method()
            ).resolveWithMib(self._mibview)
            binds.append(mib_var)

        if trap_name in self.name_to_OID['NotificationType']:
            # mpModel = 0, SNMPv1
            # mpModel = 1, SNMPv2
            err, err_status, err_idx, varbinds = next(
                hlapi.sendNotification(
                    hlapi.SnmpEngine(),
                    hlapi.CommunityData('public', mpModel=1),
                    hlapi.UdpTransportTarget((self.target, 162)),
                    hlapi.ContextData(),
                    'trap',    # or inform
                    hlapi.NotificationType(
                        # Trap with any additional varbinds attached
                        rfc1902.ObjectIdentity(self._mib._name, trap_name).resolveWithMib(self._mibview)).addVarBinds(
                            *binds)
                )
            )

            if not err:
                log.debug("trap sent")
            else:
                log.error("trap send failure")
                log.error(err)
                raise
        else:
            log.warning("trap '{}' requested that doesn't exist in the MIB".format(trap_name))

    def attach_responders(self):
        # Add the responders to pysnmp for get, getnext, and getbulk
        log.debug("Attaching pysnmp responders")
        cmdrsp.GetCommandResponder(self._engine, self._context)
        cmdrsp.NextCommandResponder(self._engine, self._context)
        cmdrsp.BulkCommandResponder(self._engine, self._context)

    def configure_listener(self):
        # open a UDP socket to listen for snmp requests
        try:
            config.addSocketTransport(self._engine, udp.domainName,
                                      udp.UdpTransport().openServerMode(('', 161)))
        except (OSError, CarrierError) as e:
            # Unable to bind to socket
            log.error("Error attempting to start listener: {}".format(e.__context__))
            self.healthy = False
            return

        # add a v2 user with the community string public
        config.addV1System(self._engine, "agent", "public")
        # let anyone accessing 'public' read anything in the subtree below,
        # which is the enterprises subtree that we defined our MIB to be in
        config.addVacmUser(self._engine, 2, "agent", "noAuthNoPriv",
                           readSubTree=(1, 3, 6, 1, 4, 1))

    def load_mib(self):
        # the builder is used to load compiled mibs and handle our
        # exports later if needed
        log.debug("loading MIB")
        mibBuilder = self._context.getMibInstrum().getMibBuilder()
        mibSources = mibBuilder.getMibSources() + (builder.DirMibSource('.'),)
        mibBuilder.setMibSources(*mibSources)
        mibBuilder.loadTexts = True
        mibBuilder.loadModule(self._mib._name)  # expects a pysnmp compiled version

        # Get the object type we'll use when linking OID to handler methods
        MibScalarInstance, = mibBuilder.importSymbols('SNMPv2-SMI',
                                                      'MibScalarInstance')

        # need a copy of the symbols since we're adding entries
        mib_symbols = mibBuilder.mibSymbols[self._mib._name].copy()
        for symbol_name in mib_symbols:
            node = mib_symbols[symbol_name]
            node_type = node.__class__.__name__

            if node_type not in self.valid_types:
                continue

            # convert the oid tuple to a string
            oid_str = '.'.join([str(n) for n in node.name])
            self.name_to_OID[node_type][node.label] = oid_str

            if node_type == 'MibScalar':
                method = getattr(self._mib, node.label, None)
                if method:
                    instance = createVariable(MibScalarInstance,
                                              method,
                                              node.name, (0,),
                                              node.syntax)

                    # need to export as <var name>Instance
                    instanceDict = {str(node.name)+"Instance": instance}
                    mibBuilder.exportSymbols(self._mib._name,
                                             **instanceDict)
                else:
                    log.warning("MIB handler missing for MIB object {}".format(node.label))

        # we need the mibview to allow the creation of varbind PDUs from our mib
        self._mibview = view.MibViewController(mibBuilder)

    def run(self):
        """start the responders (listener) to handle to GET/WALK etc"""
        log.info("SNMP listener starting")

        self.configure_listener()

        if self.healthy:
            self.attach_responders()

            self._engine.transportDispatcher.jobStarted(1)
            try:
                self._engine.transportDispatcher.runDispatcher()
            except:
                self.shutdown()
                raise
        else:
            log.error("SNMP listener aborting")

    def shutdown(self):
        log.info("SNMP listener shutting down")
        self._engine.transportDispatcher.closeDispatcher()


class MIBHandler(object):
    """Handler class to respond to GET/GETNEXT/WALK/TABLE requests"""

    def __init__(self, ceph_cluster, filename, filepath='/usr/share/snmp/mibs'):
        self._lock = threading.RLock()
        self._filename = filename
        self._filepath = filepath
        self._ceph = ceph_cluster
        self._name = filename[:-4]  # strip the .txt from the filename

        self.compilation_success = self.compile()  # attempt a compilation of the mib

    def compile(self):
        """Attempt a compilation of the MIB, output is placed in cwd"""

        log.debug("compiling MIB")
        input_MIBs = [self._filename]
        src_dirs = [self._filepath]
        dst_dir = './'
        mib_compiler = MibCompiler(SmiStarParser(),
                                   PySnmpCodeGen(),
                                   PyFileWriter(dst_dir))

        # search for source MIBs here
        mib_compiler.addSources(*[FileReader(x) for x in src_dirs])

        # check compiled MIBs in our own directories
        mib_compiler.addSearchers(PyFileSearcher(dst_dir))
        # ...and at default PySNMP MIBs packages
        mib_compiler.addSearchers(*[PyPackageSearcher(x) for x in
                                    PySnmpCodeGen.defaultMibPackages])

        # never recompile MIBs with MACROs
        mib_compiler.addSearchers(StubSearcher(*PySnmpCodeGen.baseMibs))

        # run [possibly recursive] MIB compilation
        results = mib_compiler.compile(*input_MIBs)  # rebuild=True, genTexts=Tr
        if self._filename in results:
            # e.g. {'MY-MIB.txt': 'failed'}
            # the compile failed
            log.debug("MIB compilation failed")
            return False
        else:
            # e.g. {'MY-MIB': 'untouched', 'SNMPv2-CONF': 'untouched', 'SNMPv2-SMI': 'untouched', 'SNMPv2-TC': 'untouched'}
            return True

    @property
    def valid(self):

        if not os.path.exists(os.path.join(self._filepath, self._filename)):
            return False

        if not self.compilation_success:
            return False

        return True

    # Methods that correspond to the scalar definitions in the MIB
    def fsid(self):
        with self._lock:
            return self._ceph.fsid

    def statusMsg(self):
        with self._lock:
            return self._ceph.statusMsg

    def health(self):
        with self._lock:
            return self._ceph.health


def createVariable(SuperClass, getValue, *args):
    """Helper function to create a instance variable that we can export.
    getValue is a function to call to retreive the value of a scalar
    during an SNMP request
    """
    class Var(SuperClass):
        def readGet(self, name, *args):
            return name, self.syntax.clone(getValue())
    return Var(*args)


# mimic the staticmethod that should be in MgrModule
def can_run():
    if not pysnmp_imported:
        return False, "pysnmp package can not be imported"
    elif not pysmi_imported:
        return False, "pysmi package can not be imported"
    elif not os.path.exists(os.path.join(Module.MIB_filepath, Module.MIB_filename)):
        return False, "{} not found in {}".format(Module.MIB_filename, Module.MIB_filepath)
    else:
        return True, ""


class Module(object):
    """Mimic the presence of the MgrModule class"""
    MIB_filename = 'MY-MIB.txt'
    MIB_filepath = '/usr/share/snmp/mibs'


class CephCluster(object):
    """Mimic a class that would hold the interesting ceph cluster stats"""

    def __init__(self):
        self.fsid = 'wah'
        self.statusMsg = "hello"
        self.health = "OK"


def main():
    handler = logging.StreamHandler(sys.stdout)
    log.setLevel(logging.DEBUG)
    log.addHandler(handler)

    # let's default to supporting a snmp get from our agent
    listen = True

    # create an object that represents the cluster, this would be updated
    # as changes in the cluster are detected
    ceph_cluster = CephCluster()

    mib = MIBHandler(ceph_cluster, Module.MIB_filename, Module.MIB_filepath)
    if not mib.valid:
        log.error("MIB is invalid")
        sys.exit(1)

    agent = SNMPAgent(mib)
    if listen:
        agent.start()

    # keep main thread alive
    log.debug("Main loop running, waiting for CTRL-C")
    try:
        while True:
            time.sleep(3)
            agent.send_trap('testTrap', var_binds=["fsid"])
    except KeyboardInterrupt:
        if agent.is_alive():
            agent.shutdown()


if __name__ == '__main__':
    runnable, msg = can_run()
    if runnable:
        main()
    else:
        print("Unable to run the test : {}".format(msg))

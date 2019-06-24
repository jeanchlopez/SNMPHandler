# SNMP agent backend e.g. Agent access to Managed Objects
from pysnmp.smi import builder, view, compiler, error, instrum, exval
from pysnmp import debug

debug.setLogger(debug.Debug('dsp'))

#from pysnmp.smi import builder, instrum, exval

# Create MIB loader/builder
mibBuilder = builder.MibBuilder()

# Optionally attach PySMI MIB compiler (if installed)
#print('Attaching MIB compiler...'),
#compiler.addMibCompiler(mibBuilder, sources=['/usr/share/snmp/mibs/', 'http://mibs.snmplabs.com/asn1/'])
#print('done')

# Optionally set an alternative path to compiled MIBs
#print('Setting MIB sources...')
#mibBuilder.addMibSources(builder.DirMibSource('/usr/share/snmp/mibs/'))
#print(mibBuilder.getMibSources())
#print('done')

print('Loading MIB modules...'),
mibBuilder.loadModules('SNMPv2-MIB', 'SNMP-FRAMEWORK-MIB', 'SNMP-COMMUNITY-MIB', 'IP-MIB', 'SNMPHANDLER-MIB')
print('done')

print('Indexing MIB objects...'),
mibView = view.MibViewController(mibBuilder)
print('done')

print('Building MIB tree...'),
mibInstrum = instrum.MibInstrumController(mibBuilder)
print('done')
#
# MonMap test
#
print('Building table entry index from human-friendly representation...'),
snmpMonMapEntry, = mibBuilder.importSymbols(
    'SNMPHANDLER-MIB', 'snmpMonMapEntry'
)

instanceId = snmpMonMapEntry.getInstIdFromIndices('10.0.1.105')
print('done')

print('Create/update SNMPHANDLER-MIB::snmpMonMapEntry table row: ')
varBinds = mibInstrum.writeVars(
    ((snmpMonMapEntry.name + (1,) + instanceId, '10.0.1.105'),
     (snmpMonMapEntry.name + (2,) + instanceId, 'n1'),
     (snmpMonMapEntry.name + (3,) + instanceId, '0'),
     (snmpMonMapEntry.name + (4,) + instanceId, '6789'),
     (snmpMonMapEntry.name + (5,) + instanceId, '0'))
)

#for oid, val in varBinds:
#    print('%s = %s' % ('.'.join([str(x) for x in oid]), not val.isValue and 'N/A' or val.prettyPrint()))
#print('done')
#
# OSDMap test
#
print('Building table entry index from human-friendly representation...'),
snmpOsdMapEntry, = mibBuilder.importSymbols(
    'SNMPHANDLER-MIB', 'snmpOsdMapEntry'
)

instanceId = snmpOsdMapEntry.getInstIdFromIndices('0')
print('done')

print('Create/update SNMPHANDLER-MIB::snmpOsdMapEntry table row: ')
varBinds = mibInstrum.writeVars(
    ((snmpOsdMapEntry.name + (1,) + instanceId, '0'),
     (snmpOsdMapEntry.name + (2,) + instanceId, '6905e6ea-a2e8-41e5-9651-1e979f07b0b3'),
     (snmpOsdMapEntry.name + (3,) + instanceId, 'n1'),
     (snmpOsdMapEntry.name + (4,) + instanceId, '1'),
     (snmpOsdMapEntry.name + (5,) + instanceId, '1'),
     (snmpOsdMapEntry.name + (6,) + instanceId, '10.0.1.105'),
     (snmpOsdMapEntry.name + (7,) + instanceId, '6804'),
     (snmpOsdMapEntry.name + (8,) + instanceId, '10.0.1.105'),
     (snmpOsdMapEntry.name + (9,) + instanceId, '6805'),
     (snmpOsdMapEntry.name + (10,) + instanceId, 'hdd'),
     (snmpOsdMapEntry.name + (11,) + instanceId, '1'),
     (snmpOsdMapEntry.name + (12,) + instanceId, '0'),
     (snmpOsdMapEntry.name + (13,) + instanceId, '10000000'),
     (snmpOsdMapEntry.name + (14,) + instanceId, '1000000'),
     (snmpOsdMapEntry.name + (15,) + instanceId, '9000000'),
     (snmpOsdMapEntry.name + (16,) + instanceId, '61'),
     (snmpOsdMapEntry.name + (17,) + instanceId, '30'),
     (snmpOsdMapEntry.name + (18,) + instanceId, '1'))
)

#
# Pool test
#
print('Building table entry index from human-friendly representation...'),
snmpPoolMapEntry, = mibBuilder.importSymbols(
    'SNMPHANDLER-MIB', 'snmpPoolMapEntry'
)

instanceId = snmpPoolMapEntry.getInstIdFromIndices('6')
print('done')

print('Create/update SNMPHANDLER-MIB::snmpPoolMapEntry table row: ')
varBinds = mibInstrum.writeVars(
    ((snmpPoolMapEntry.name + (1,) + instanceId, '6'),
     (snmpPoolMapEntry.name + (2,) + instanceId, 'type1'),
     (snmpPoolMapEntry.name + (3,) + instanceId, '0'),
     (snmpPoolMapEntry.name + (4,) + instanceId, '2'),
     (snmpPoolMapEntry.name + (5,) + instanceId, '1'),
     (snmpPoolMapEntry.name + (6,) + instanceId, '0'),
     (snmpPoolMapEntry.name + (7,) + instanceId, '32'),
     (snmpPoolMapEntry.name + (8,) + instanceId, '32'),
     (snmpPoolMapEntry.name + (9,) + instanceId, '0'),
     (snmpPoolMapEntry.name + (10,) + instanceId, '0'),
     (snmpPoolMapEntry.name + (11,) + instanceId, '0'),
     (snmpPoolMapEntry.name + (12,) + instanceId, '0'),
     (snmpPoolMapEntry.name + (13,) + instanceId, '0'),
     (snmpPoolMapEntry.name + (14,) + instanceId, 'rbd'))
)

#for oid, val in varBinds:
#    print('%s = %s' % ('.'.join([str(x) for x in oid]), not val.isValue and 'N/A' or val.prettyPrint()))
#print('done')

print('Read whole MIB (table walk)')
oid, val = (), None
while True:
    oid, val = mibInstrum.readNextVars(((oid, val),))[0]
    if exval.endOfMib.isSameTypeWith(val):
        break
    print('%s = %s' % ('.'.join([str(x) for x in oid]), not val.isValue and 'N/A' or val.prettyPrint()))
print('done')

print('Unloading MIB modules...'),
mibBuilder.unloadModules()
print('done')

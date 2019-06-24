# SNMP agent backend e.g. Agent access to Managed Objects
from pysnmp.smi import builder, instrum, exval

print('Loading MIB modules...'),
mibBuilder = builder.MibBuilder().loadModules(
    'SNMPv2-MIB', 'SNMP-FRAMEWORK-MIB', 'SNMP-COMMUNITY-MIB', 'SNMPHANDLER-MIB'
)
print('done')

print('Building MIB tree...'),
mibInstrum = instrum.MibInstrumController(mibBuilder)
print('done')

print('Building table entry index from human-friendly representation...'),
snmpCommunityEntry, = mibBuilder.importSymbols(
    'SNMP-COMMUNITY-MIB', 'snmpCommunityEntry'
)
instanceId = snmpCommunityEntry.getInstIdFromIndices('my-router')
print('done')

print('Create/update SNMP-COMMUNITY-MIB::snmpCommunityEntry table row: ')
varBinds = mibInstrum.writeVars(
    ((snmpCommunityEntry.name + (2,) + instanceId, 'mycomm'),
     (snmpCommunityEntry.name + (3,) + instanceId, 'mynmsname'),
     (snmpCommunityEntry.name + (7,) + instanceId, 'volatile'))
)
for oid, val in varBinds:
    print('%s = %s' % ('.'.join([str(x) for x in oid]), not val.isValue and 'N/A' or val.prettyPrint()))
print('done')

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
#
# Test with SNMPHANDLER-MIB
#
print('Loading MIB modules...'),
mibBuilder2 = builder.MibBuilder().loadModules(
    'SNMPv2-SMI', 'SNMP-FRAMEWORK-MIB', 'SNMPv2-CONF', 'SNMPv2-MIB', 'SNMPHANDLER-MIB'
)
print('done')

print('Building MIB tree...'),
mibInstrum2 = instrum.MibInstrumController(mibBuilder2)
print('done')


print('Building table entry index from human-friendly representation...'),
snmpMonMapEntry, = mibBuilder2.importSymbols(
    'SNMPHANDLER-MIB', 'snmpMonMapEntry'
)

instanceId = snmpMonMapEntry.getInstIdFromIndices('042afa57-7bc2-451a-8880-cd0647347125')
print('done')

print('Create/update SNMPHANDLER-MIB::snmpMonMapEntry table row: ')
varBinds = mibInstrum2.writeVars(
    ((snmpMonMapEntry.name + (1,) + instanceId, '10.0.1.105'),
     (snmpMonMapEntry.name + (2,) + instanceId, '10.0.1.106'),
     (snmpMonMapEntry.name + (3,) + instanceId, '10.0.1.107'))
)

for oid, val in varBinds:
    print('%s = %s' % ('.'.join([str(x) for x in oid]), not val.isValue and 'N/A' or val.prettyPrint()))
print('done')

print('Read whole MIB (table walk)')
oid, val = (), None
while True:
    oid, val = mibInstrum2.readNextVars(((oid, val),))[0]
    if exval.endOfMib.isSameTypeWith(val):
        break
    print('%s = %s' % ('.'.join([str(x) for x in oid]), not val.isValue and 'N/A' or val.prettyPrint()))
print('done')

print('Unloading MIB modules...'),
mibBuilder2.unloadModules()
print('done')

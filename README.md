# SNMPHandler Module

This initial version uses the CLI to generate the traps it is sending for portability reasons when I started working on it
This made it possible to handle easily the support for any version of SNMP (V1 through V3)

The files
* __init__.py		(module initaliazation)
* module.py		(The code itself)
* types.py		(The types needed for the module. Imported from other modules for later expansion)
* SNMPHANDLER-MIB.txt	(The MIB source code so it can be imported into snmptrapd and used in snmptrap making it easier)

## Installation

Deploy the source code in /usr/lib64/ceph/mgr/snmphandler
Copy the MIB source file to /usr/share/snmp/mibs (e.g. snmptrap -m +SNMPHANDLER-MIB ... or snmptranslate -m +SNMPHANDLER-MIB -IR  -On fsId)
Restart the snmptrapd and smpd daemons
Enable the module with ceph mgr module enable snmphandler

## Features
* SNMP version and parameters can be customized via the ceph config-key set mgr/snmphandler/{parameter} {value}
     *  snmp_version	Can be set to 1, 2c or 3. Default is 1.
     *  snmp_community	Community for SNMP V1 and V2. Default is public.
     *  snmpv3_level        For V3 can be set to noAuthNoPriv, authNoPriv and authPriv. Default is noAuthNoPriv.
     *  snmpv3_engine	For V3 we need an engine ID. Default is 0x8000000001020304
     *  snmpv3_user		For V3 we need a user when authentication is enabled. Default is ceph
     *  snmpv3_auth		For V3 we need a password and an encryption method when authentication is enabled. Default is SHA:mypassword.
     *  snmpv3_enc		For V3 we need a passphrase and an encryption method when privacy is enabled. Default is AES:mypassword.
     *  sleep_interval	The sleep in the loop portion of the code. RFU SNMP agent). Default is 30 seconds.
     *  trap_addr		Where to send the trap. For now support a single destination. Default is localhost.
     *  trap_oid		What OID to use for a test trap. Default is 1.3.6.1.4.1.50495.
     *  trap_on_shutdown	Send a trap when the module is shutting down. Default is false.
     *  trap_on_start	Send a trap when the module is coming up online. Default is false.
     *  trap_port		To what port we send the trap. Default is 162.
     *  trap_using_tools	Use the API or the snmptrap CLI to generate the trap. Default is true.
* Monitor cluster general status and sends the appropriate trap when a change occurs
* Ceph Manager failover tested and operational

What to do in the future
* Handle OSD status change in the OSD map
* Handle MGR status change in the MGR map
* Hanle MON status change in the MON map
* Handle SVC map updates and notification when new service gets deployed
* Use the SNMP standard API to initiate the traps
* Write a simple SNMP agent responding to snmpget and walk requests for discover and monitoring
* Eventually if needed extend SNMP agent support to SNMP set requests and pass them to the Ceph cluster

I have copied some test files I worked on for the future and the use of an API in the tests foler
* module-pysnmp	Test using an SNMP API module wrapper to send traps
* test-agent.py	Test to query the MIB iirc.
* tets-agent2.py	Test to query the MIB iirc.
* test-compile.py	Compile and verify the MIB iirc.
* test-snmp.py	Test to walk some OID iirc.
 


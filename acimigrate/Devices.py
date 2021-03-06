#!/usr/bin/env python
import xml.etree.ElementTree as ET
from ncclient import manager
import acitoolkit.acitoolkit as aci
from acitoolkit import Node

VLAN_POOL_NAME = 'acimigrate-vlan-pool'


class APIC(object):
    """
    ACI Utilities used for migrating to ACI fabric
    """

    def __init__(self, url, username, password):
        self.url = url
        self.username = username
        self.password = password
        self.session = aci.Session(self.url, self.username, self.password, verify_ssl=False)
        self.session.login()
        self.tenant = None
        self.app = None
        self.physdom = None
        self.context = None
        self.contract = None
        self.fabric_interfaces = aci.Interface.get(self.session)
        self.apic_migration_dict = None
        self.migration_vpc_dn = None
        self.prot_path_dn = None
        # used to track the leaves we are using, used to generate protep dn
        self.migration_leaves = []
        self.migration_vpc_rn = None

    def migration_vlan_pool(self, vlans=None):
        """
        "Creates a VLAN pool based on a list of vlans"
        :param vlans:
        :return:
        """
        print "creating vlan pool for list {}".format(vlans)

        # Initialize a list of fvnsEncapBlk
        children = []

        # construct an encap block for each VLAN
        for v in vlans:
            obj = {"fvnsEncapBlk": {
                "attributes":
                    {"allocMode": "inherit",
                     "descr": "",
                     "from": "vlan-{0}".format(v),
                     "name": "vlan-{0}".format(v),
                     "nameAlias": "",
                     "to": "vlan-{}".format(v)}}}
            children.append(obj)

        # construct vlan pool
        obj = {"fvnsVlanInstP":
                   {"attributes":
                        {"allocMode": "static",
                         "descr": "",
                         "dn": "uni/infra/vlanns-[{}]-static".format(VLAN_POOL_NAME),
                         "name": "{}".format(VLAN_POOL_NAME),
                         }, "children": children
                    }
               }

        # commit vlan pool to APIC
        resp = self.session.push_to_apic('/api/mo/uni/infra.json', obj)

        # return the dn of the object
        return obj['fvnsVlanInstP']['attributes']['dn']

    def node_id_from_name(self, name):
        """
        Returns a node id from name
        :param name:
        :return: node_id

        """
        resp = self.session.get('/api/node/class/fabricNode.json?'
                                'query-target-filter=and(eq(fabricNode.name,"{}"))'.format(name))

        return resp.json()['imdata'][0]['fabricNode']['attributes']['id']

    def create_node_profile(self, switchname, selector):
        """
        creates a switch profile by name

        :param switchname:
        :return:
        """
        node_id = self.node_id_from_name(switchname)

        self.migration_leaves.append(node_id)
        node_prof_json = {"infraNodeP":
                              {"attributes":
                                   {"dn": "uni/infra/nprof-{}".format(switchname),
                                    "name": switchname,
                                    "rn": "nprof-{}".format(switchname)},
                               "children": [
                                   {"infraRsAccPortP": {"attributes": {"tDn": selector}}},
                                   {"infraLeafS":
                                        {"attributes":
                                             {"dn": "uni/infra/nprof-{0}/leaves-{0}-typ-range".format(switchname),
                                              "type": "range",
                                              "name": switchname
                                              },
                                         "children": [
                                             {"infraNodeBlk":
                                                  {"attributes":
                                                       {"dn": "uni/infra/nprof-{0}/"
                                                              "leaves-{0}-typ-range/nodeblk-{0}".format(switchname),
                                                        "from_": node_id,
                                                        "to_": node_id,
                                                        "name": switchname,
                                                        }
                                                   }
                                              }
                                         ]
                                         }
                                    }
                               ]
                               }
                          }
        resp = self.session.push_to_apic('/api/mo/uni/infra.json', node_prof_json)
        print resp.text

    def infraPortBlk(self, portprofdn, port):
        """
        create infraPortBlk Json
        :param portprofdn: dn of the portprofile
        :param port:
        :return:
        """
        infraportblk = {
            "infraPortBlk":
                {"attributes":
                    {
                        "dn": "{}/hports-ints-typ-range/portblk-port{}".format(portprofdn, port),
                        "fromPort": port,
                        "toPort": port,
                        "name": "port{}".format(port)
                    }
                }
        }
        return infraportblk

    def port_num_from_name(self, name):
        """
        returns just port number from interface Name
        :param name: e.g Eth1/5
        :return: str 5
        """
        print name
        return name.split('/')[1]

    def create_interface_selector(self):
        info = self.apic_migration_dict
        # Creates Interface Selector for for each switch
        for switch in info.keys():
            dn = 'uni/infra/accportprof-{}-intselector'.format(switch)
            interface_selectors = {"infraAccPortP":
                                       {"attributes":
                                            {"dn": dn,
                                             "name": "{}-intselector".format(switch)
                                             },
                                        "children": [{"infraHPortS":
                                            {"attributes": {
                                                "name": "ints",
                                                "type": "range"
                                            }

                                            }
                                        }

                                        ]
                                        }}

            # Here we are getting just the port number info[switch] is a list of lists, so we need to break it down
            names = map(lambda p: p[0], info[switch])
            ports = [self.port_num_from_name(n) for n in names]

            # Add portblk for each interface
            children = [self.infraPortBlk(dn, p) for p in ports]

            # Also need to associate policy-group
            policy_group = {"infraRsAccBaseGrp": {"attributes": {"tDn": self.migration_vpc_dn}}}
            children.append(policy_group)

            interface_selectors['infraAccPortP']['children'][0]['infraHPortS']['children'] = children

            print interface_selectors
            resp = self.session.push_to_apic('/api/mo/uni.json', interface_selectors)

            # Now we associate the
            self.create_node_profile(switch, dn)
            print resp.text

    def create_10G_link_policy(self, name):
        """
        This creates the 10G link policy for later
        :param name: name for the interface-policy
        :return: str dn of the created object
        """
        obj = {"fabricHIfPol": {"attributes":
                                    {"autoNeg": "on",
                                     "descr": "",
                                     "dn": "uni/infra/hintfpol-{}".format(name),
                                     "fecMode": "inherit",
                                     "linkDebounce": "100",
                                     "name": "{}".format(name),
                                     "nameAlias": "",
                                     "speed": "10G"}}}

        resp = self.session.push_to_apic('/api/mo/uni.json', obj)
        return obj['fabricHIfPol']['attributes']['name']

    def create_lacp_policy(self, name):
        obj = {"lacpLagPol":
                   {"attributes":
                        {"ctrl": "fast-sel-hot-stdby,graceful-conv,susp-individual",
                         "descr": "",
                         "dn": "uni/infra/lacplagp-{}".format(name),
                         "maxLinks": "16",
                         "minLinks": "1",
                         "mode": "active",
                         "name": "{}".format(name)
                         }
                    }
               }
        resp = self.session.push_to_apic('/api/mo/uni.json', obj)
        return obj['lacpLagPol']['attributes']['name']

    def create_cdp_policies(self, name):
        obj = {"cdpIfPol":
                   {"attributes":
                        {"adminSt": "enabled",
                         "descr": "",
                         "dn": "uni/infra/cdpIfP-{}".format(name),
                         "name": "{}".format(name),
                         }
                    }
               }
        resp = self.session.push_to_apic('/api/mo/uni/infra.json', obj)
        return obj['cdpIfPol']['attributes']['name']

    def create_aep(self, name):
        obj = {"infraAttEntityP":
                   {"attributes":
                        {"descr": "",
                         "dn": "uni/infra/attentp-{}".format(name),
                         "name": "{}".format(name)
                         },
                    "children": [{"infraRsDomP": {"attributes": {"tDn": "uni/phys-{}".format(self.physdom)}}}]}}
        print obj
        resp = self.session.push_to_apic('/api/mo/uni/infra.json', obj)
        return obj['infraAttEntityP']['attributes']['dn']

    def create_vpc_policy_group(self, name):
        """
        This creates the VPC policy group for migration, as part of this, it also will creates the following policies for
        migration
        * LACP Policy
        * CDP Policy
        * Link Level (10G)

        :param name: name for the VPC
        :return: str dn of the policy group

        """

        # Create the necessary policies for constructing the policy group
        cdp = self.create_cdp_policies('acimigrate-cdp-policy')
        lacp = self.create_lacp_policy('acimigrate-lacp-policy')
        link = self.create_10G_link_policy('aci-migrate-link-policy')
        aep = self.create_aep('acimigrate-aep')

        obj = {"infraAccBndlGrp":
                   {"attributes":
                        {"dn": "uni/infra/funcprof/accbundle-{}".format(name),
                         "lagT": "node",
                         "name": "{}".format(name)
                         },
                    "children": [
                        {"infraRsAttEntP": {"attributes": {"tDn": aep}}},
                        {"infraRsCdpIfPol": {"attributes":
                                                 {"tnCdpIfPolName": cdp}}},
                        {"infraRsHIfPol":
                             {
                                 "attributes":
                                     {
                                         "tnFabricHIfPolName": link}}},
                        {"infraRsLacpPol":
                             {
                                 "attributes":
                                     {"tnLacpLagPolName": lacp}}},
                    ]
                    }
               }

        # Update the dn of the migration vpc so that it can be used later
        self.migration_vpc_dn = obj['infraAccBndlGrp']['attributes']['dn']
        self.migration_vpc_rn = name
        resp = self.session.push_to_apic('/api/mo/uni/infra/funcprof.json', obj)
        return self.migration_vpc_dn

    def migration_physdom(self, domain_name, vlans):
        """
        Create a physdom for migration connectivity
        :param domain_name:
        :param vlans:
        :return:
        """
        self.physdom = domain_name
        pool_dn = self.migration_vlan_pool(vlans=vlans)
        dom_json = {"physDomP":
                        {"attributes":
                             {"dn": "uni/phys-{}".format(self.physdom),
                              "name": self.physdom
                              },
                         "children": [{"infraRsVlanNs":
                             {"attributes": {
                                 "tDn": "{}".format(pool_dn),
                                 "status": "created"}, "children": []}}]}}
        print "Creating Physical Domain {}".format(self.physdom)
        resp = self.session.push_to_apic('/api/mo/uni.json', dom_json)
        print resp.text

    def migration_tenant(self, tenant_name, app_name, provision=True):
        self.tenant = aci.Tenant(tenant_name)
        print self.tenant.get_url()
        self.app = aci.AppProfile(app_name, self.tenant)
        self.context = aci.Context('default', self.tenant)

        self.contract = aci.Contract('allow-any', self.tenant)
        entry1 = aci.FilterEntry('default',
                                 applyToFrag='no',
                                 arpOpc='unspecified',
                                 etherT='unspecified',
                                 parent=self.contract)
        if provision:
            self.session.push_to_apic(self.tenant.get_url(), self.tenant.get_json())
        else:
            self.tenant.get_json()
        return self.tenant

    def create_epg_for_vlan(self, name, num, mac_address=None, net=None, provision=True):
        """
        This creates the EPG for a given EPG, it is generally called from the main migration routine

        :param name: str name for the vlan
        :param num: str vlan id
        :param mac_address: str
        :param net: str
        :param provision: bool
        :return:
        """

        epg = aci.EPG(name, self.app)
        bd = aci.BridgeDomain(name, self.tenant)

        if net:
            subnet = aci.Subnet('subnet-' + name, parent=bd)
            subnet.set_addr(net)
            bd.set_unicast_route('yes')
        else:
            bd.set_unicast_route('no')

        if mac_address:
            bd.set_mac(mac_address)

        bd.set_unknown_mac_unicast('flood')
        bd.set_arp_flood('yes')
        bd.add_context(self.context)
        epg.add_bd(bd)
        epg.provide(self.contract)
        epg.consume(self.contract)
        # Attach physdom
        dom = aci.EPGDomain('acimigrate', epg)
        dom.tDn = 'uni/phys-{}'.format(self.physdom)

        if provision:
            resp = self.session.push_to_apic(self.tenant.get_url(), self.tenant.get_json())
            print resp.text
            # Add static binding for migration interface
            self.migration_leaves = sorted(self.migration_leaves)
            protep_str = "topology/pod-1/protpaths-{}-{}/pathep-[{}]".format(self.migration_leaves[0],
                                                                             self.migration_leaves[1],
                                                                             self.migration_vpc_rn)

            c = {"fvRsPathAtt": {"attributes": {"encap": "vlan-{}".format(num),
                                                "tDn": protep_str,
                                                "status": "created"},
                                 "children": []}}

            epgurl = '/api/mo/uni/tn-{}/ap-{}/epg-{}.json'.format(self.tenant,
                                                                  self.app,
                                                                  epg)
            print "Creating static path binding for {}....".format(protep_str),

            bindresp = self.session.push_to_apic(epgurl, c)
            print bindresp.status_code

        else:
            print self.tenant.get_json()

        return resp

    def list_switches(self):
        phy_class = (Node)
        switches = phy_class.get(self.session)
        return switches

    def get_switch_interfaces(self, node):
        int_list = []
        for int in self.fabric_interfaces:
            if int.node == node:
                int_list.append(int)
        return int_list


class Nexus(object):
    """
    Class for gleaning useful information from an NX-OS device

    """

    def __init__(self, host, user, passwd):
        self.host = host
        self.user = user
        self.passwd = passwd
        self.port = 22
        self.hostkey_verify = False
        self.device_params = {'name': 'nexus'}
        self.allow_agent = False
        self.look_for_keys = False
        self.manager = manager.connect(host=self.host,
                                       port=22,
                                       username=self.user,
                                       password=self.passwd,
                                       hostkey_verify=False,
                                       device_params={'name': 'nexus'},
                                       allow_agent=False,
                                       look_for_keys=False)

    cmd_default_int_snippet = """
        <default>
            <interface>
                <__XML__value>%s</__XML__value>
            </interface>
          </default>
          """

    cmd_config_pc_trunk = """
            <interface>
              <__XML__value>%s</__XML__value>
            </interface>
              <description>
                  <__XML__value>acimigrate-intf</__XML__value>
              </description>
              <__XML__value>switchport mode trunk</__XML__value>
              <__XML__value>channel-group %s mode active</__XML__value>
        """

    cmd_config_vpc_member = """
        <interface>
            <__XML__value>port-channel%s</__XML__value>
        </interface>
            <vpc>
                <__XML__value>%s</__XML__value>
            </vpc>
        """

    exec_conf_prefix = """
      <config xmlns:xc="urn:ietf:params:xml:ns:netconf:base:1.0">
        <configure xmlns="http://www.cisco.com/nxos:1.0:vlan_mgr_cli">
          <__XML__MODE__exec_configure>
    """

    exec_conf_postfix = """
              </__XML__MODE__exec_configure>
            </configure>
          </config>
            """

    cmd_vlan_conf_snippet = """
                <vlan>
                  <vlan-id-create-delete>
                    <__XML__PARAM_value>%s</__XML__PARAM_value>
                    <__XML__MODE_vlan>
                      <name>
                        <vlan-name>%s</vlan-name>
                      </name>
                      <state>
                        <vstate>active</vstate>
                      </state>
                      <no>
                        <shutdown/>
                      </no>
                    </__XML__MODE_vlan>
                  </vlan-id-create-delete>
                </vlan>
    """

    cmd_vlan_int_snippet = """
              <interface>
                <ethernet>
                  <interface>%s</interface>
                  <__XML__MODE_if-ethernet-switch>
                    %s
                  </__XML__MODE_if-ethernet-switch>
                </ethernet>
              </interface>
    """

    cmd_no_vlan_int_snippet = """
          <config xmlns:xc="urn:ietf:params:xml:ns:netconf:base:1.0">
            <configure xmlns="http://www.cisco.com/nxos:1.0:vlan_mgr_cli">
              <__XML__MODE__exec_configure>
              <interface>
                <ethernet>
                  <interface>%s</interface>
                  <__XML__MODE_if-ethernet-switch>
                    <switchport>
                      <trunk>
                        <allowed>
                          <vlan>
                            <__XML__BLK_Cmd_switchport_trunk_allowed_allow-vlans>
                              <remove-vlans>%s</remove-vlans>
                            </__XML__BLK_Cmd_switchport_trunk_allowed_allow-vlans>
                          </vlan>
                        </allowed>
                      </trunk>
                    </switchport>
                  </__XML__MODE_if-ethernet-switch>
                </ethernet>
              </interface>
              </__XML__MODE__exec_configure>
            </configure>
          </config>
    """

    cmd_vlan_common = """
                <switchport>
                  <trunk>
                    <allowed>
                      <vlan>
                        <add>
                        <__XML__BLK_Cmd_switchport_trunk_allowed_allow-vlans>
                          <add-vlans>%s</add-vlans>
                        </__XML__BLK_Cmd_switchport_trunk_allowed_allow-vlans>
                        </add>
                      </vlan>
                    </allowed>
                  </trunk>
                </switchport>
                """

    cmd_vlan_pc_snippet = """
          <interface>
            <Port-Channel>
              <interface>%s</interface>
              <__XML__MODE_if-eth-port-channel-switch>
                %s
              </__XML__MODE_if-eth-port-channel-switch>
            </ethernet>
          </interface>
    """

    filter_show_vlan_brief_snippet = """
          <show xmlns="http://www.cisco.com/nxos:1.0:vlan_mgr_cli">
            <vlan>
              <brief/>
            </vlan>
          </show> """

    @staticmethod
    def format_mac_address(mac):
        """
        Re-format IOS mac addresses
        :param mac: string mac address in 0000.0000.0000 format
        :return: string 00:00:00:00:00
        """
        return '{0}:{1}:{2}:{3}:{4}:{5}'.format(mac[:2],
                                                mac[2:4],
                                                mac[5:7],
                                                mac[7:9],
                                                mac[10:12],
                                                mac[12:14])

    @property
    def port_channel_dict(self):
        query = '''
            <show>
                <port-channel>
                    <summary/>
                </port-channel>
            </show>
            '''
        ncdata = str(self.manager.get(('subtree', query)))
        root = ET.fromstring(ncdata)
        pc_ns_map = {'groups': 'http://www.cisco.com/nxos:1.0:eth_pcm_dc3'}
        pc_dict = {}

        for c in root.iter():
            pcs = (c.findall('groups:ROW_channel', pc_ns_map))
            for pc in pcs:
                portchannel = pc.find('groups:group', pc_ns_map).text
                member_list = []
                for row in pc:
                    ints = row.findall('groups:ROW_member', pc_ns_map)
                    for int in ints:
                        interface = int.find('groups:port', pc_ns_map).text
                        member_list.append(interface)
                pc_dict[portchannel] = member_list
        # print pc_dict
        return pc_dict

    @property
    def vpc_dict(self):
        query = '''
            <show>
                <vpc/>
            </show>
            '''
        ncdata = str(self.manager.get(('subtree', query)))
        root = ET.fromstring(ncdata)
        vpc_ns_map = {'groups': 'http://www.cisco.com/nxos:1.0:mcecm'}
        vpc_dict = {}
        vpc_id_list = []

        for c in root.iter():
            vpcs = (c.findall('groups:ROW_vpc', vpc_ns_map))
            for vpc in vpcs:
                vpc_id = vpc.find('groups:vpc-id', vpc_ns_map).text
                vpc_id_list.append(vpc_id)
        vpc_dict["vpc_list"] = vpc_id_list
        print vpc_dict
        return vpc_dict

    @property
    def phy_interface_dict(self):
        query = '''
            <show>
                <interface>
                    <status/>
                </interface>
            </show>
        '''

        ncdata = str(self.manager.get(('subtree', query)))
        root = ET.fromstring(ncdata)
        int_ns_map = {'groups': 'http://www.cisco.com/nxos:1.0:if_manager'}
        int_list = []

        for c in root.iter():
            ints = (c.findall('groups:ROW_interface', int_ns_map))
            for int in ints:
                interface = int.find('groups:interface', int_ns_map).text
                if interface.startswith("Ethernet"):
                    int_list.append(interface)

        return int_list

    @property
    def vlan_dict(self):
        query = '''
              <show>
                <vlan/>
              </show>
        '''

        ncdata = str(self.manager.get(('subtree', query)))
        root = ET.fromstring(ncdata)
        namespace_map = {'vlans': 'http://www.cisco.com/nxos:1.0:vlan_mgr_cli'}
        vlan_dict = {}

        for c in root.iter():
            vlans = c.findall('vlans:ROW_vlanbrief', namespace_map)
            for v in vlans:
                vlanid = v.find('vlans:vlanshowbr-vlanid-utf', namespace_map).text
                vlan_name = v.find('vlans:vlanshowbr-vlanname', namespace_map).text
                vlan_dict[vlanid] = vlan_name

        return vlan_dict

    @property
    def svi_dict(self):
        query = '''
            <show>
              <ip>
                <interface/>
            </show>
        '''

        ncdata = str(self.manager.get(('subtree', query)))
        root = ET.fromstring(ncdata)
        svi_ns_map = {'groups': 'http://www.cisco.com/nxos:1.0:ip'}
        svi_dict = {}

        for c in root.iter():
            svi_intfs = (c.findall('groups:ROW_intf', svi_ns_map))
            for i in svi_intfs:
                subnet_list = []
                mask_list = []
                intf = i.find('groups:intf-name', svi_ns_map).text
                subnet = i.find('groups:subnet', svi_ns_map).text
                subnet_list.append(subnet)
                mask = i.find('groups:masklen', svi_ns_map).text
                mask_list.append(mask)

                secondaries = i.find('groups:TABLE_secondary_address', svi_ns_map)
                if secondaries is not None:
                    count = 0
                    for sec in secondaries.iter():
                        count = count + 1
                        # print count
                        # print sec
                        rows = sec.getchildren()
                        for row in rows:
                            # print row.attrib
                            subnetx = row.find('groups:subnet' + str(count), svi_ns_map)
                            if subnetx is not None:
                                subnetx = subnetx.text
                                subnet_list.append(subnetx)
                            maskx = row.find('groups:masklen' + str(count), svi_ns_map)
                            if maskx is not None:
                                maskx = maskx.text
                                mask_list.append(maskx)

                svi_dict[intf] = {'subnets': subnet_list, 'masks': mask_list}
        # print "svi_dict: " , svi_dict
        return svi_dict

    @property
    def hsrp_dict(self):
        query = '''
                  <show>
                    <hsrp>
                        <detail/>
                    </hsrp>
                  </show>
                      '''
        ncdata = str(self.manager.get(('subtree', query)))
        root = ET.fromstring(ncdata)
        hsrp_ns_map = {'groups': 'http://www.cisco.com/nxos:1.0:hsrp_engine'}
        hsrp_dict = {}

        for c in root.iter():
            hsrp_intfs = (c.findall('groups:ROW_grp_detail', hsrp_ns_map))
            for i in hsrp_intfs:
                vip_list = []
                intf = i.find('groups:sh_if_index', hsrp_ns_map).text
                vip = i.find('groups:sh_vip', hsrp_ns_map).text
                vip_list.append(vip)
                mac = i.find('groups:sh_vmac', hsrp_ns_map).text

                # Check for secondary HSRP addresses
                secondaries = i.find('groups:TABLE_grp_vip_sec', hsrp_ns_map)
                if secondaries is not None:
                    for sec in secondaries.iter():
                        ips = sec.findall('groups:sh_vip_sec', hsrp_ns_map)
                        for ip in ips:
                            vip_list.append(ip.text)

                hsrp_dict[intf] = {'vmac': self.format_mac_address(mac),
                                   'vips': vip_list}
        return hsrp_dict

    def enable_vlan(self, vlanid, vlanname):
        confstr = self.cmd_vlan_conf_snippet % (vlanid, vlanname)
        confstr = self.exec_conf_prefix + confstr + self.exec_conf_postfix
        self.manager.edit_config(target='running', config=confstr)

    def enable_vlan_on_trunk_int(self, interface, vlanid):
        switchport = self.cmd_vlan_common % vlanid
        if '/' in interface:
            confstr = self.cmd_vlan_int_snippet % (interface, switchport)
        else:
            confstr = self.cmd_vlan_pc_snippet % (interface, switchport)
        confstr = self.exec_conf_prefix + confstr + self.exec_conf_postfix
        self.manager.edit_config(target='running', config=confstr)

    def enable_vlan_on_trunk_pc(self, interface, vlanid):
        switchport = self.cmd_vlan_common % vlanid
        confstr = self.cmd_vlan_pc_snippet % (interface,
                                              switchport)

        confstr = self.exec_conf_prefix + confstr + self.exec_conf_postfix
        self.manager.edit_config(target='running', config=confstr)

    def disable_vlan_on_trunk_int(self, interface, vlanid):
        confstr = self.cmd_no_vlan_int_snippet % (interface, vlanid)
        print confstr
        self.manager.edit_config(target='running', config=confstr)

    def build_xml(self, cmd):
        args = cmd.split(' ')
        xml = ""
        for a in reversed(args):
            xml = """<%s>%s</%s>""" % (a, xml, a)
        return xml

    def run_cmd(self, cmd):
        xml = self.build_xml(cmd)
        ncdata = str(self.manager.get(('subtree', xml)))
        return ncdata

    def migration_dict(self):
        """
        Merges Nexus.vlan_dict and Nexus.hsrp_dict

        """
        migrate_dict = {}
        migrate_dict['vlans'] = {}
        for v in self.vlan_dict.keys():
            migrate_dict['vlans'][v] = {'name': self.vlan_dict[v]}
            if 'Vlan{0}'.format(v) in self.hsrp_dict.keys():
                migrate_dict['vlans'][v]['hsrp'] = self.hsrp_dict['Vlan{0}'.format(v)]
                migrate_dict['vlans'][v]['hsrp'].update(self.svi_dict['Vlan{0}'.format(v)])
            else:
                migrate_dict['vlans'][v]['hsrp'] = None
        # migrate_dict['interfaces'] = self.free_interfaces()
        # print self.port_channel_dict
        # print self.free_interfaces()
        return migrate_dict

    def pc_list(self):

        pc_list = []
        for pc in self.port_channel_dict.keys():
            pc_list.append(pc)
        return pc_list

    def free_interfaces(self):
        """
        Removes interfaces that are currently in use by existing port-channels
        :return:
        """
        used_int_list = []
        for pc in self.port_channel_dict:
            for int in self.port_channel_dict[pc]:
                used_int_list.append(int)

        # print used_int_list

        free_int_list = [x for x in self.phy_interface_dict if x not in used_int_list]
        # free_int_list = set(self.phy_interface_dict) - set(used_int_list)
        # print free_int_list
        return free_int_list

    def cdp_neighbors(self):
        query = self.build_xml('show cdp neighbor')
        ncdata = str(self.manager.get(('subtree', query)))
        root = ET.fromstring(ncdata)
        neighbors = {}
        cdp_ns_map = {'mod': 'http://www.cisco.com/nxos:1.0:cdpd'}
        for c in root.iter(tag='{http://www.cisco.com/nxos:1.0:cdpd}ROW_cdp_neighbor_brief_info'):
            neighbor = c.find('mod:device_id', cdp_ns_map).text
            myintf = c.find('mod:intf_id', cdp_ns_map).text
            neigh_intf = c.find('mod:port_id', cdp_ns_map).text
            platform = c.find('mod:platform_id', cdp_ns_map).text
            neighbor = neighbor.split('(')[0]

            neighbors[neighbor] = {'local_intf': myintf,
                                   'neighbor_intf': neigh_intf,
                                   'platform': platform,
                                   }
        return neighbors

    def config_phy_connection(self, interfaces, pc):
        """
        Expects a list of interfaces and port channel number
        with matching unused numbers
        :param interfaces:
        :return:
        """
        for interface in interfaces:
            default = self.cmd_default_int_snippet % interface
            port_config = self.cmd_config_pc_trunk % (interface, pc)

            confstr = default + port_config
            confstr = self.exec_conf_prefix + confstr + self.exec_conf_postfix
            self.manager.edit_config(target='running', config=confstr)

        confstr = self.cmd_config_vpc_member % (pc, pc)
        confstr = self.exec_conf_prefix + confstr + self.exec_conf_postfix
        self.manager.edit_config(target='running', config=confstr)

        status = True
        return status


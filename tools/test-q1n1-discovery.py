#!/usr/bin/env python3
"""Reader regressions: cable-independent inventory, failed endpoints and peers."""
import struct
import plistlib
import subprocess
import unittest
from unittest.mock import Mock, patch
import q1n1proxy as q
import udplink as u


class DiscoveryTests(unittest.TestCase):
    def test_ncm_names_are_recursive_and_numeric(self):
        nodes = [{'IORegistryEntryChildren': [{'BSD Name': 'en12'}, {'BSD Name': 'anpi0'},
                  {'IORegistryEntryChildren': [{'BSD Name': 'en4'}, {'BSD Name': 'en12'}]}]}]
        self.assertEqual(u.ncm_interface_names(nodes), ['en4', 'en12'])

    def test_both_transports_discovered(self):
        with patch.object(q, 'find_ports', return_value=['/dev/cu.A', '/dev/cu.B']), \
             patch.object(u, 'find_interfaces', return_value=['en4', 'en12']):
            self.assertEqual(q.discover_endpoints(), ['/dev/cu.A', '/dev/cu.B', 'udp://en4', 'udp://en12'])
            self.assertEqual(q.discover_endpoints(interface=q.CONSOLE_INTERFACE), ['/dev/cu.A', '/dev/cu.B'])

    def test_removed_interface_does_not_hide_remaining_port(self):
        inventory = plistlib.dumps([{'BSD Name': 'en4'}, {'BSD Name': 'en5'}])
        with patch.object(u.subprocess, 'run', side_effect=[Mock(stdout=inventory),
                subprocess.CalledProcessError(1, ['ifconfig', 'en4']), Mock(stdout='status: active')]):
            self.assertEqual(u.find_interfaces(), ['en5'])

    def test_stale_first_endpoint_does_not_hide_second(self):
        first, second = Mock(), Mock()
        second.proxyreq.return_value = struct.pack('<QqQ', 0, 0, 0)
        first.nop.side_effect = TimeoutError('stale tty')
        first.close.side_effect = OSError('gone')
        with patch.object(q, 'discover_endpoints', return_value=['/stale', '/working']), \
             patch.object(q.Path, 'exists', return_value=True), \
             patch.object(q, 'Link', side_effect=[first, second]):
            proxy = q.connect()
        self.assertEqual(proxy.endpoint, '/working')
        first.close.assert_called_once()
        second.nop.assert_called_once_with(0)

    def test_old_bootinfo_does_not_overread(self):
        data = struct.pack('<49Q', q.MAGIC, 1, 392, *([0] * 46))
        link = Mock()
        link.readmem.side_effect = lambda address, size: data[:size]
        proxy = q.Proxy(link)
        proxy.request = Mock(return_value=0x1000)
        self.assertEqual(proxy.bootinfo()['usb_ports'], 0)
        self.assertEqual([c.args[1] for c in link.readmem.call_args_list], [24, 392])

    def test_invalid_bootinfo_rejected_before_body_read(self):
        link = Mock()
        link.readmem.return_value = struct.pack('<3Q', q.MAGIC, 1, 4097)
        proxy = q.Proxy(link)
        proxy.request = Mock(return_value=0x1000)
        with self.assertRaises(q.ProxyError):
            proxy.bootinfo()
        link.readmem.assert_called_once()

    def test_udp_rejects_other_ports_addresses_and_scopes(self):
        transport = u.UdpTransport.__new__(u.UdpTransport)
        transport.socket = Mock()
        transport.socket.recvfrom.side_effect = [
            (b'wrong-port', ('fe80::4919', 1234, 0, 7)),
            (b'wrong-address', ('fe80::1111', 4919, 0, 7)),
            (b'other-cable', ('fe80::4919', 4919, 0, 8)),
            (b'valid', ('fe80::4919', 4919, 0, 7)), BlockingIOError()]
        transport.port, transport.scope, transport.address = 4919, 7, 'fe80::4919'
        transport.buffer, transport.frames_in = bytearray(), 0
        self.assertEqual(transport._harvest(), 1)
        self.assertEqual(transport.buffer, b'valid')

    def test_chainload_requires_compatible_live_ncm_state(self):
        proxy = q.Proxy(Mock())
        state = struct.pack('<10Q', 0xa600000, 1, 1, *([0] * 7))
        contract = struct.pack('<3Q', 0x325354524f505355, 1, 25000)
        proxy.readmem = Mock(side_effect=lambda address, size, **kw: state if size == 80 else contract)
        info = {'usb_ports': 0x2018, 'usb_port_count': 1}
        image = struct.pack('<6Q', 0, q.STAGE_MAGIC, 0xb0000000, 0,
                            0x3154525055533151, 0xb0000030) + contract
        proxy.check_stage_transport(image, info)
        with self.assertRaisesRegex(q.ProxyError, 'incompatible'):
            proxy.check_stage_transport(image[:-8] + struct.pack('<Q', 25008), info)
        with self.assertRaisesRegex(q.ProxyError, 'no USB handoff contract'):
            proxy.check_stage_transport(image[:32], info)


if __name__ == '__main__':
    unittest.main()

#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Host-side xHCI driver for the A16's USB1 controller, over the q1n1 proxy.

Bring-up runs from here rather than from target C, the way m1n1's proxyclient
drives Apple hardware: every register access is a proxy request, so a change
costs a millisecond instead of a rebuild-and-chainload cycle. Once a sequence
is proven here it can be ported into a stage.

USB1 (0x0a800000) is a DWC_usb31 core that this machine's firmware leaves in
host mode (GCTL.PRTCAPDIR = 1) and halted, with no driver bound in Windows, so
q1n1 can own it outright. Port 1 is USB2, port 2 is USB3.1; they are the two
halves of the same Type-C connector -- the one with the bare cable to the Mac.

DMA is identity-mapped and cache-coherent on this SoC: the DWC3 gadget driver
services its rings with a bare `dmb` and no cache maintenance, so the proxy's
ordinary writes are visible to the controller.
"""
import argparse
import struct
import sys
import time

sys.path.insert(0, __file__.rsplit('/', 1)[0])
import q1n1proxy as q1n1

USB1_BASE = 0x0A800000
USB0_BASE = 0x0A600000

# Operational registers (offsets from `op`).
USBCMD, USBSTS, PAGESIZE, DNCTRL, CRCR, DCBAAP, CONFIG = 0x00, 0x04, 0x08, 0x14, 0x18, 0x30, 0x38
CMD_RS, CMD_HCRST, CMD_INTE, CMD_HSEE = 1 << 0, 1 << 1, 1 << 2, 1 << 3
STS_HCH, STS_HSE, STS_EINT, STS_PCD, STS_CNR, STS_HCE = 1 << 0, 1 << 2, 1 << 3, 1 << 4, 1 << 11, 1 << 12

# Interrupter 0 (offsets from `runtime` + 0x20).
IMAN, IMOD, ERSTSZ, ERSTBA, ERDP = 0x00, 0x04, 0x08, 0x10, 0x18

# PORTSC bits.
P_CCS, P_PED, P_OCA, P_PR, P_PP = 1 << 0, 1 << 1, 1 << 3, 1 << 4, 1 << 9
P_LWS, P_CSC, P_PEC, P_WRC, P_OCC, P_PRC, P_PLC, P_CEC = (1 << 16, 1 << 17, 1 << 18, 1 << 19,
                                                          1 << 20, 1 << 21, 1 << 22, 1 << 23)
P_WPR = 1 << 31
# Writing PORTSC must not disturb the write-1-to-clear change bits or PED
# (which is write-1-to-*disable*), so every read-modify-write masks them out.
PORTSC_RW = ~(P_PED | P_CSC | P_PEC | P_WRC | P_OCC | P_PRC | P_PLC | P_CEC) & 0xFFFFFFFF

PLS_NAMES = {0: 'U0', 1: 'U1', 2: 'U2', 3: 'U3', 4: 'Disabled', 5: 'RxDetect', 6: 'Inactive',
             7: 'Polling', 8: 'Recovery', 9: 'HotReset', 10: 'Compliance', 11: 'Test'}
SPEED_NAMES = {0: '-', 1: 'Full', 2: 'Low', 3: 'High', 4: 'Super', 5: 'SuperPlus'}

TRB_NAMES = {
    1: 'Normal', 2: 'Setup', 3: 'Data', 4: 'Status', 6: 'Link',
    9: 'EnableSlot', 10: 'DisableSlot', 11: 'AddressDevice', 12: 'ConfigureEndpoint',
    13: 'EvaluateContext', 14: 'ResetEndpoint', 16: 'NoOpCommand', 23: 'NoOpCommand',
    32: 'TransferEvent', 33: 'CommandCompletion', 34: 'PortStatusChange', 37: 'HostController',
}
COMPLETION = {
    0: 'Invalid', 1: 'Success', 2: 'DataBufferError', 3: 'BabbleDetected', 4: 'USBTransaction',
    5: 'TRBError', 6: 'StallError', 7: 'ResourceError', 8: 'BandwidthError', 9: 'NoSlotsAvailable',
    11: 'SlotNotEnabled', 13: 'ShortPacket', 17: 'ParameterError', 19: 'ContextStateError',
    21: 'CommandRingStopped', 22: 'CommandAborted', 23: 'Stopped', 25: 'ControlSequenceError',
}

# The command ring closes with a Link TRB, so one of its slots is not usable.
# The event ring has no Link TRB: the controller wraps using the segment size
# in the ERST, so every slot counts and the two sizes must be stated apart.
CMD_RING_TRBS = 256
EVENT_RING_TRBS = 256
TRB_SIZE = 16


def trb_type(control):
    return (control >> 10) & 0x3F


class XHCIError(RuntimeError):
    pass


class XHCI:
    """xHCI host controller driven over the proxy.

    Layout in the DMA window (default: 1 MiB carved out of the proxy heap,
    which no target code uses):

        +0x00000  DCBAA           device context base address array
        +0x01000  command ring
        +0x02000  event ring segment 0
        +0x03000  ERST
        +0x04000  scratchpad pointer array
        +0x05000  scratchpad buffers
        +0x10000  free for device contexts and transfer rings
    """

    def __init__(self, proxy, base=USB1_BASE, dma=None, dma_size=1 << 20, verbose=True):
        self.p = proxy
        self.base = base
        self.verbose = verbose
        info = proxy.bootinfo()
        if info.get('usb_ports'):
            raise XHCIError('both controllers are managed by q1n1; raw takeover would corrupt live DMA')
        if ((proxy.read32(base + 0xC110) >> 12) & 3) == 2 and proxy.read32(base + 0xC704) & (1 << 31):
            raise XHCIError('refusing raw takeover of an active USB device controller')
        if dma is None:
            # Top of the heap, 64 KiB aligned; the heap is host scratch only.
            dma = (info['heap_base'] + info['heap_size'] - dma_size) & ~0xFFFF
        self.dma = dma
        self.dma_size = dma_size
        self.dcbaa = dma + 0x0000
        self.cmd_ring = dma + 0x1000
        self.event_ring = dma + 0x2000
        self.erst = dma + 0x3000
        self.scratch_array = dma + 0x4000
        self.scratch_buffers = dma + 0x5000
        self.free = dma + 0x10000
        self.read_caps()
        self.cmd_index = 0
        self.cmd_cycle = 1
        self.event_index = 0
        self.event_cycle = 1

    # ---- register access -------------------------------------------------
    def log(self, *args):
        if self.verbose:
            print(*args)

    def read_caps(self):
        cap = self.p.read32(self.base)
        self.caplength, self.hciversion = cap & 0xFF, (cap >> 16) & 0xFFFF
        self.hcsparams1 = self.p.read32(self.base + 0x04)
        self.hcsparams2 = self.p.read32(self.base + 0x08)
        self.hccparams1 = self.p.read32(self.base + 0x10)
        self.dboff = self.p.read32(self.base + 0x14) & ~0x3
        self.rtsoff = self.p.read32(self.base + 0x18) & ~0x1F
        self.op = self.base + self.caplength
        self.runtime = self.base + self.rtsoff
        self.db = self.base + self.dboff
        self.ir0 = self.runtime + 0x20
        self.max_slots = self.hcsparams1 & 0xFF
        self.max_ports = (self.hcsparams1 >> 24) & 0xFF
        self.context_size = 64 if (self.hccparams1 >> 2) & 1 else 32
        self.ac64 = self.hccparams1 & 1
        self.scratchpads = (((self.hcsparams2 >> 21) & 0x1F) << 5) | ((self.hcsparams2 >> 27) & 0x1F)
        self.page_size = 0

    def op_rd(self, off):
        return self.p.read32(self.op + off)

    def op_wr(self, off, value):
        self.p.write32(self.op + off, value)

    def portsc(self, port):
        return self.p.read32(self.op + 0x400 + 0x10 * (port - 1))

    def portsc_write(self, port, value):
        self.p.write32(self.op + 0x400 + 0x10 * (port - 1), value)

    def portsc_set(self, port, bits):
        """Read-modify-write that leaves the write-1-to-clear bits alone."""
        value = (self.portsc(port) & PORTSC_RW) | bits
        self.portsc_write(port, value)

    def portsc_ack(self, port, bits):
        """Acknowledge change bits without altering anything else."""
        self.portsc_write(port, (self.portsc(port) & PORTSC_RW) | bits)

    @staticmethod
    def describe_port(value):
        return (f'{value:#010x} ccs={value & 1} ped={(value >> 1) & 1} pr={(value >> 4) & 1} '
                f'pls={PLS_NAMES.get((value >> 5) & 0xF, (value >> 5) & 0xF)} '
                f'pp={(value >> 9) & 1} '
                f'speed={SPEED_NAMES.get((value >> 10) & 0xF, (value >> 10) & 0xF)} '
                f'csc={(value >> 17) & 1} pec={(value >> 18) & 1} wrc={(value >> 19) & 1} '
                f'prc={(value >> 21) & 1} plc={(value >> 22) & 1}')

    def wait(self, read, mask, want, timeout=1.0, what='register'):
        deadline = time.monotonic() + timeout
        while True:
            value = read()
            if (value & mask) == want:
                return value
            if time.monotonic() > deadline:
                raise XHCIError(f'timed out waiting for {what} (last {value:#010x})')

    # ---- controller bring-up --------------------------------------------
    def mode(self):
        """DWC3 port capability direction: 1 = host, 2 = device."""
        return (self.p.read32(self.base + 0xC110) >> 12) & 3

    def halted(self):
        return bool(self.op_rd(USBSTS) & STS_HCH)

    def stop(self, timeout=2.0):
        if not self.halted():
            self.op_wr(USBCMD, self.op_rd(USBCMD) & ~CMD_RS)
            self.wait(lambda: self.op_rd(USBSTS), STS_HCH, STS_HCH, timeout, 'the controller to halt')

    def reset(self, timeout=2.0):
        """Halt, then HCRST, then wait out Controller Not Ready."""
        self.stop(timeout)
        self.op_wr(USBCMD, CMD_HCRST)
        self.wait(lambda: self.op_rd(USBCMD), CMD_HCRST, 0, timeout, 'HCRST to clear')
        self.wait(lambda: self.op_rd(USBSTS), STS_CNR, 0, timeout, 'CNR to clear')
        self.page_size = (self.op_rd(PAGESIZE) & 0xFFFF) << 12
        self.log(f'  reset done: USBCMD {self.op_rd(USBCMD):#010x} USBSTS {self.op_rd(USBSTS):#010x} '
                 f'page size {self.page_size}')

    def zero(self, address, size):
        self.p.memset8(address, 0, size)

    def build_rings(self):
        """DCBAA, scratchpad, command ring, event ring. All zeroed first."""
        self.zero(self.dma, 0x10000)

        # Scratchpad buffers: the controller needs them before it will run.
        if self.scratchpads:
            entries = b''
            for n in range(self.scratchpads):
                entries += struct.pack('<Q', self.scratch_buffers + n * self.page_size)
            self.p.writemem(self.scratch_array, entries)
            self.zero(self.scratch_buffers, self.scratchpads * self.page_size)
            self.p.write64(self.dcbaa, self.scratch_array)

        # Command ring: a Link TRB in the last slot points back at the start,
        # with Toggle Cycle set so the producer cycle bit flips each lap.
        link = struct.pack('<QII', self.cmd_ring, 0, (6 << 10) | (1 << 1) | self.cmd_cycle)
        self.p.writemem(self.cmd_ring + (CMD_RING_TRBS - 1) * TRB_SIZE, link)

        # Event ring: one segment, described by a one-entry ERST.
        self.p.writemem(self.erst, struct.pack('<QII', self.event_ring, EVENT_RING_TRBS, 0))

        self.p.write64(self.op + DCBAAP, self.dcbaa)
        self.op_wr(CONFIG, self.max_slots)
        self.p.write64(self.op + CRCR, self.cmd_ring | self.cmd_cycle)

        self.p.write32(self.ir0 + ERSTSZ, 1)
        self.p.write64(self.ir0 + ERDP, self.event_ring)
        self.p.write64(self.ir0 + ERSTBA, self.erst)
        self.p.write32(self.ir0 + IMOD, 0)
        self.log(f'  rings: dcbaa {self.dcbaa:#x} cmd {self.cmd_ring:#x} '
                 f'event {self.event_ring:#x} erst {self.erst:#x} '
                 f'scratchpads {self.scratchpads}')

    def start(self, timeout=2.0):
        """Run/Stop, with interrupts left masked -- the host polls instead."""
        self.op_wr(USBCMD, self.op_rd(USBCMD) | CMD_RS)
        self.wait(lambda: self.op_rd(USBSTS), STS_HCH, 0, timeout, 'the controller to run')
        self.log(f'  running: USBCMD {self.op_rd(USBCMD):#010x} USBSTS {self.op_rd(USBSTS):#010x} '
                 f'CRCR {self.p.read64(self.op + CRCR):#x}')

    def init(self):
        self.log(f'xHCI @ {self.base:#x}: version {self.hciversion >> 8}.{(self.hciversion >> 4) & 0xF}, '
                 f'{self.max_slots} slots, {self.max_ports} ports, {self.context_size}-byte contexts')
        mode = self.mode()
        if mode != 1:
            raise XHCIError(f'DWC3 is not in host mode (GCTL.PRTCAPDIR = {mode})')
        self.reset()
        self.build_rings()
        self.start()

    # ---- rings -----------------------------------------------------------
    def post_command(self, parameter=0, status=0, control=0):
        """Write a TRB into the command ring and ring doorbell 0."""
        control = (control & ~1) | self.cmd_cycle
        address = self.cmd_ring + self.cmd_index * TRB_SIZE
        self.p.writemem(address, struct.pack('<QII', parameter, status, control))
        self.cmd_index += 1
        if self.cmd_index == CMD_RING_TRBS - 1:  # the Link TRB closes the ring
            link = struct.pack('<QII', self.cmd_ring, 0, (6 << 10) | (1 << 1) | self.cmd_cycle)
            self.p.writemem(self.cmd_ring + (CMD_RING_TRBS - 1) * TRB_SIZE, link)
            self.cmd_index = 0
            self.cmd_cycle ^= 1
        self.p.write32(self.db, 0)
        return address

    def poll_events(self, limit=16):
        """Consume every event whose cycle bit matches ours."""
        events = []
        for _ in range(limit):
            address = self.event_ring + self.event_index * TRB_SIZE
            parameter, status, control = struct.unpack('<QII', self.p.readmem(address, TRB_SIZE))
            if (control & 1) != self.event_cycle:
                break
            events.append({'parameter': parameter, 'status': status, 'control': control,
                           'type': trb_type(control), 'code': (status >> 24) & 0xFF})
            self.event_index += 1
            if self.event_index == EVENT_RING_TRBS:
                self.event_index = 0
                self.event_cycle ^= 1
        if events:
            dequeue = self.event_ring + self.event_index * TRB_SIZE
            self.p.write64(self.ir0 + ERDP, dequeue | (1 << 3))   # EHB: clear the busy bit
        return events

    def describe_event(self, event):
        name = TRB_NAMES.get(event['type'], f"type {event['type']}")
        code = COMPLETION.get(event['code'], event['code'])
        if event['type'] == 34:
            return f"PortStatusChange port {(event['parameter'] >> 24) & 0xFF} ({code})"
        if event['type'] == 33:
            return f"CommandCompletion {code} trb {event['parameter']:#x} slot {(event['control'] >> 24) & 0xFF}"
        if event['type'] == 32:
            return (f"TransferEvent {code} slot {(event['control'] >> 24) & 0xFF} "
                    f"ep {(event['control'] >> 16) & 0x1F} residual {event['status'] & 0xFFFFFF}")
        return f"{name} code {code} parameter {event['parameter']:#x} status {event['status']:#010x}"

    def drain(self, timeout=0.3, limit=32):
        """Collect events for a fixed window."""
        deadline = time.monotonic() + timeout
        collected = []
        while time.monotonic() < deadline and len(collected) < limit:
            events = self.poll_events(limit)
            collected += events
            if not events:
                time.sleep(0.01)
        return collected

    def command(self, parameter=0, status=0, control=0, timeout=1.0):
        """Post a command and wait for its Command Completion Event."""
        address = self.post_command(parameter, status, control)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for event in self.poll_events():
                if event['type'] == 33 and event['parameter'] == address:
                    return event
                self.log(f'    (while waiting) {self.describe_event(event)}')
            time.sleep(0.005)
        raise XHCIError(f'no completion for command TRB at {address:#x} '
                        f'(USBSTS {self.op_rd(USBSTS):#010x}, CRCR {self.p.read64(self.op + CRCR):#x})')

    def no_op(self, timeout=1.0):
        return self.command(control=23 << 10, timeout=timeout)

    # ---- supported protocols --------------------------------------------
    def protocols(self):
        """Walk the extended capabilities for Supported Protocol entries."""
        found = []
        offset = (self.hccparams1 >> 16) & 0xFFFF
        if not offset:
            return found
        pointer = self.base + offset * 4
        for _ in range(32):
            word = self.p.read32(pointer)
            if word == 0 or word == 0xFFFFFFFF:
                break
            if (word & 0xFF) == 2:
                name = self.p.read32(pointer + 4)
                ports = self.p.read32(pointer + 8)
                slot = self.p.read32(pointer + 12)
                found.append({
                    'major': (word >> 24) & 0xFF, 'minor': (word >> 16) & 0xFF,
                    'name': bytes((name >> (8 * i)) & 0xFF for i in range(4)).decode('latin1'),
                    'first': ports & 0xFF, 'count': (ports >> 8) & 0xFF,
                    'slot_type': slot & 0x1F,
                })
            nxt = (word >> 8) & 0xFF
            if not nxt:
                break
            pointer += nxt * 4
        return found

    def slot_type_for_port(self, port):
        for entry in self.protocols():
            if entry['first'] <= port < entry['first'] + entry['count']:
                return entry['slot_type']
        return 0

    # ---- device contexts and enumeration --------------------------------
    def context_offset(self, dci, input_context=True):
        """Input contexts are shifted by one: slot at 1, EP0 at 2."""
        return self.context_size * (dci + 1 if input_context else dci)

    def address_device(self, port, speed, slot_type=0, block=False):
        """Enable a slot, build its contexts, and address the device."""
        event = self.command(control=(9 << 10) | (slot_type << 16))
        if event['code'] != 1:
            raise XHCIError(f'Enable Slot failed: {COMPLETION.get(event["code"], event["code"])}')
        slot = (event['control'] >> 24) & 0xFF
        self.log(f'  slot {slot} enabled')

        device_context = self.free + 0x0000
        input_context = self.free + 0x1000
        ep0_ring = self.free + 0x2000
        self.zero(self.free, 0x3000 + 0x1000)

        # EP0 transfer ring, closed with a Link TRB back to its own start.
        self.p.writemem(ep0_ring + (CMD_RING_TRBS - 1) * TRB_SIZE,
                        struct.pack('<QII', ep0_ring, 0, (6 << 10) | (1 << 1) | 1))

        # Input Control Context: add the slot context (A0) and EP0 (A1).
        self.p.writemem(input_context, struct.pack('<II', 0, 0b11))

        # Slot Context: one context entry, this root hub port, this speed.
        slot_ctx = struct.pack('<IIII',
                               (speed & 0xF) << 20 | (1 << 27),   # Speed, Context Entries = 1
                               (port & 0xFF) << 16,               # Root Hub Port Number
                               0, 0)
        self.p.writemem(input_context + self.context_offset(0), slot_ctx)

        # EP0 Context: control endpoint, CErr 3, max packet 64 at high speed.
        packet = {1: 64, 2: 8, 3: 64, 4: 512, 5: 512}.get(speed, 64)
        ep0_ctx = struct.pack('<IIQII',
                              0,
                              (3 << 1) | (4 << 3) | (packet << 16),  # CErr, EP type 4, MPS
                              ep0_ring | 1,                          # TR dequeue + DCS
                              8, 0)                                  # Average TRB length
        self.p.writemem(input_context + self.context_offset(1), ep0_ctx)

        self.p.write64(self.dcbaa + 8 * slot, device_context)

        control = (11 << 10) | (slot << 24) | ((1 << 9) if block else 0)
        event = self.command(parameter=input_context, control=control, timeout=2.0)
        if event['code'] != 1:
            self.disable_slot(slot)
            raise XHCIError(f'Address Device failed: '
                            f'{COMPLETION.get(event["code"], event["code"])} (code {event["code"]})')
        state = struct.unpack('<I', self.p.readmem(device_context + 12, 4))[0]
        self.log(f'  addressed: USB address {state & 0xFF}, slot state {(state >> 27) & 0x1F}')
        return {'slot': slot, 'device_context': device_context, 'input_context': input_context,
                'ep0_ring': ep0_ring, 'packet': packet, 'index': 0, 'cycle': 1, 'rings': {}}

    def release(self, device, data_interface=None):
        """Hand the device back in an orderly way.

        Resetting the controller out from under a configured device leaves the
        far end wedged -- an Apple device stops chirping for high speed and
        stops answering control transfers until it is physically replugged --
        so unwind the configuration before letting go.
        """
        try:
            if data_interface is not None:
                self.set_interface(device, data_interface, 0)
            self.set_configuration(device, 0)
        except XHCIError as error:
            self.log(f'  teardown: {error}')
        self.disable_slot(device['slot'])

    def disable_slot(self, slot):
        try:
            self.command(control=(10 << 10) | (slot << 24), timeout=1.0)
        except XHCIError:
            pass

    def control_transfer(self, device, request_type, request, value, index, length,
                         buffer=None, timeout=2.0):
        """Setup/Data/Status TRBs on EP0, then wait for the Transfer Event."""
        if buffer is None:
            buffer = self.free + 0x3000
        setup = struct.pack('<BBHHH', request_type, request, value, index, length)
        incoming = bool(request_type & 0x80)
        trt = 0 if length == 0 else (3 if incoming else 2)

        trbs = [(struct.unpack('<Q', setup)[0], 8, (2 << 10) | (trt << 16) | (1 << 6))]
        if length:
            trbs.append((buffer, length, (3 << 10) | ((1 << 16) if incoming else 0)))
        trbs.append((0, 0, (4 << 10) | (0 if incoming else (1 << 16)) | (1 << 5)))

        if length:
            self.zero(buffer, (length + 63) & ~63)

        first = None
        for parameter, status, control in trbs:
            address = device['ep0_ring'] + device['index'] * TRB_SIZE
            first = first if first is not None else address
            self.p.writemem(address, struct.pack('<QII', parameter, status,
                                                 (control & ~1) | device['cycle']))
            device['index'] += 1
            if device['index'] == CMD_RING_TRBS - 1:
                self.p.writemem(device['ep0_ring'] + (CMD_RING_TRBS - 1) * TRB_SIZE,
                                struct.pack('<QII', device['ep0_ring'], 0,
                                            (6 << 10) | (1 << 1) | device['cycle']))
                device['index'] = 0
                device['cycle'] ^= 1

        self.p.write32(self.db + 4 * device['slot'], 1)     # doorbell: EP0 (DCI 1)

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for event in self.poll_events():
                if event['type'] == 32:
                    residual = event['status'] & 0xFFFFFF
                    if event['code'] not in (1, 13):
                        raise XHCIError(f'control transfer failed: '
                                        f'{COMPLETION.get(event["code"], event["code"])}')
                    got = length - residual if length else 0
                    data = self.p.readmem(buffer, got) if got else b''
                    return data
                self.log(f'    (while waiting) {self.describe_event(event)}')
            time.sleep(0.005)
        raise XHCIError(f'no transfer event (USBSTS {self.op_rd(USBSTS):#010x})')

    # ---- configuring endpoints ------------------------------------------
    def endpoint_ring(self, device, dci):
        """One 4 KiB transfer ring per DCI, closed with a Link TRB."""
        ring = device['rings'].get(dci)
        if ring is None:
            base = self.free + 0x4000 + 0x1000 * (dci - 2)
            self.zero(base, 0x1000)
            self.p.writemem(base + (CMD_RING_TRBS - 1) * TRB_SIZE,
                            struct.pack('<QII', base, 0, (6 << 10) | (1 << 1) | 1))
            ring = device['rings'][dci] = {'base': base, 'index': 0, 'cycle': 1}
        return ring

    def configure_endpoints(self, device, endpoints):
        """Configure Endpoint command: give the controller the bulk contexts.

        `endpoints` is a list of (dci, ep_type, max_packet). This is what has to
        happen before SET_INTERFACE, the same order Linux's xhci-hcd uses.
        """
        input_context = device['input_context']
        self.zero(input_context, self.context_size * 33)

        add = 1                       # A0: the slot context is being changed
        highest = 1
        for dci, ep_type, packet in endpoints:
            add |= 1 << dci
            highest = max(highest, dci)
            ring = self.endpoint_ring(device, dci)
            context = struct.pack('<IIQII',
                                  0,
                                  (3 << 1) | (ep_type << 3) | (packet << 16),
                                  ring['base'] | ring['cycle'],
                                  packet, 0)
            self.p.writemem(input_context + self.context_offset(dci), context)
        self.p.writemem(input_context, struct.pack('<II', 0, add))

        # Copy the addressed slot context forward, raising Context Entries.
        slot = bytearray(self.p.readmem(device['device_context'], 32))
        word = struct.unpack('<I', slot[0:4])[0]
        word = (word & 0x07FFFFFF) | (highest << 27)
        slot[0:4] = struct.pack('<I', word)
        self.p.writemem(input_context + self.context_offset(0), bytes(slot))

        event = self.command(parameter=input_context,
                             control=(12 << 10) | (device['slot'] << 24), timeout=2.0)
        if event['code'] != 1:
            raise XHCIError(f'Configure Endpoint failed: '
                            f'{COMPLETION.get(event["code"], event["code"])}')
        self.log(f'  endpoints configured: ' +
                 ', '.join(f'DCI {dci}' for dci, _, _ in endpoints))
        return event

    def post_transfer(self, device, dci, buffer, length):
        """Queue one Normal TRB and ring the doorbell; returns the TRB address."""
        ring = self.endpoint_ring(device, dci)
        address = ring['base'] + ring['index'] * TRB_SIZE
        control = (1 << 10) | (1 << 5) | (1 << 2)      # Normal, IOC, ISP
        self.p.writemem(address, struct.pack('<QII', buffer, length,
                                             (control & ~1) | ring['cycle']))
        ring['index'] += 1
        if ring['index'] == CMD_RING_TRBS - 1:
            self.p.writemem(ring['base'] + (CMD_RING_TRBS - 1) * TRB_SIZE,
                            struct.pack('<QII', ring['base'], 0,
                                        (6 << 10) | (1 << 1) | ring['cycle']))
            ring['index'] = 0
            ring['cycle'] ^= 1
        self.p.write32(self.db + 4 * device['slot'], dci)
        return address

    def await_transfer(self, dci, address, length, timeout=2.0):
        """Wait for one queued TRB. Returns bytes transferred, or None if it is
        still outstanding -- the caller must not queue another for that ring."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for event in self.poll_events():
                if event['type'] == 32 and event['parameter'] == address:
                    if event['code'] not in (1, 13):
                        raise XHCIError(f'transfer on DCI {dci} failed: '
                                        f'{COMPLETION.get(event["code"], event["code"])}')
                    return length - (event['status'] & 0xFFFFFF)
                self.log(f'    (while waiting) {self.describe_event(event)}')
            time.sleep(0.005)
        return None

    def transfer(self, device, dci, buffer, length, timeout=2.0, direction_in=True):
        """One Normal TRB, waited on to completion."""
        address = self.post_transfer(device, dci, buffer, length)
        got = self.await_transfer(dci, address, length, timeout)
        if got is None:
            raise XHCIError(f'no transfer event on DCI {dci}')
        return self.p.readmem(buffer, got) if direction_in and got else got

    def receive(self, device, dci, buffer, size, timeout=2.0):
        """Bulk IN that keeps at most one TRB outstanding across timeouts."""
        pending = device.setdefault('pending', {})
        if pending.get(dci) is None:
            pending[dci] = self.post_transfer(device, dci, buffer, size)
        got = self.await_transfer(dci, pending[dci], size, timeout)
        if got is None:
            return None
        pending[dci] = None
        return self.p.readmem(buffer, got) if got else b''

    def send(self, device, dci, buffer, data, timeout=2.0):
        """Bulk OUT, with the zero-length packet a full-size block needs."""
        self.p.writemem(buffer, data)
        self.transfer(device, dci, buffer, len(data), timeout, direction_in=False)
        if len(data) and len(data) % 512 == 0:
            self.transfer(device, dci, buffer, 0, timeout, direction_in=False)
        return len(data)

    # ---- standard and class requests ------------------------------------
    def set_configuration(self, device, value):
        self.control_transfer(device, 0x00, 9, value, 0, 0)

    def set_interface(self, device, interface, alternate):
        self.control_transfer(device, 0x01, 11, alternate, interface, 0)

    def get_ntb_parameters(self, device, interface):
        return self.control_transfer(device, 0xA1, 0x80, 0, interface, 28)

    def set_packet_filter(self, device, interface, filter_bits=0x0F):
        self.control_transfer(device, 0x21, 0x43, filter_bits, interface, 0)

    def get_descriptor(self, device, kind, index, length, language=0):
        return self.control_transfer(device, 0x80, 6, (kind << 8) | index, language, length)

    def get_string(self, device, index, language=0x0409):
        if not index:
            return ''
        head = self.get_descriptor(device, 3, index, 4, language)
        if len(head) < 2 or head[1] != 3:
            return ''
        data = self.get_descriptor(device, 3, index, head[0], language)
        return data[2:].decode('utf-16-le', 'replace')

    # ---- ports -----------------------------------------------------------
    def reset_port(self, port, warm=False, timeout=1.0):
        """Reset a port and wait for PRC/WRC. A USB3 port takes a warm reset."""
        bit = P_WPR if warm else P_PR
        self.portsc_set(port, bit)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = self.portsc(port)
            if value & (P_PRC | P_WRC):
                return value
            time.sleep(0.01)
        return self.portsc(port)

    def port_report(self, port):
        return f'  port {port}: {self.describe_port(self.portsc(port))}'


def open_proxy(args):
    return q1n1.connect(device=args.device, debug=args.debug)


def cmd_survey(args):
    proxy = open_proxy(args)
    for name, base in (('USB0 (dock)', USB0_BASE), ('USB1 (Mac cable)', USB1_BASE)):
        xhci = XHCI(proxy, base, verbose=False)
        mode = {1: 'host', 2: 'device', 3: 'otg'}.get(xhci.mode(), '?')
        print(f'{name} @ {base:#x}: DWC3 {mode} mode, '
              f'xHCI {"halted" if xhci.halted() else "running"}, '
              f'{xhci.max_ports} ports, USBCMD {xhci.op_rd(USBCMD):#010x} '
              f'USBSTS {xhci.op_rd(USBSTS):#010x}')
        for port in range(1, xhci.max_ports + 1):
            print(xhci.port_report(port))


def cmd_init(args):
    if args.base == USB0_BASE:
        print('REFUSED: USB0 carries this proxy console; resetting it would cut the link.',
              file=sys.stderr)
        return 1
    proxy = open_proxy(args)
    xhci = XHCI(proxy, args.base)
    print('--- before ---')
    for port in range(1, xhci.max_ports + 1):
        print(xhci.port_report(port))

    print('--- init ---')
    xhci.init()

    print('--- no-op command (proves DMA, command ring and event ring) ---')
    event = xhci.no_op()
    print(f'  {xhci.describe_event(event)}')
    if event['code'] != 1:
        raise XHCIError(f'no-op command failed: {COMPLETION.get(event["code"], event["code"])}')

    print('--- ports after start ---')
    for event in xhci.drain(0.3):
        print(f'  event: {xhci.describe_event(event)}')
    for port in range(1, xhci.max_ports + 1):
        print(xhci.port_report(port))

    if args.reset_ports:
        for port in args.reset_ports:
            warm = port == 2
            print(f'--- {"warm " if warm else ""}reset port {port} ---')
            value = xhci.reset_port(port, warm=warm)
            print(f'  after reset: {xhci.describe_port(value)}')
            xhci.portsc_ack(port, P_PRC | P_WRC | P_CSC | P_PEC | P_PLC)
            time.sleep(0.1)
            print(xhci.port_report(port))
            for event in xhci.drain(0.3):
                print(f'  event: {xhci.describe_event(event)}')

    print('--- final ---')
    print(f'  USBSTS {xhci.op_rd(USBSTS):#010x}  '
          f'CRCR {proxy.read64(xhci.op + CRCR):#x}  '
          f'ERDP {proxy.read64(xhci.ir0 + ERDP):#x}')
    for port in range(1, xhci.max_ports + 1):
        print(xhci.port_report(port))


def cmd_bringup(args):
    """init, wait for a device, reset its port, and report what attached."""
    if args.base == USB0_BASE:
        print('REFUSED: USB0 carries this proxy console.', file=sys.stderr)
        return 1
    proxy = open_proxy(args)
    xhci = XHCI(proxy, args.base)

    print('--- init ---')
    xhci.init()
    event = xhci.no_op()
    if event['code'] != 1:
        raise XHCIError(f'no-op failed: {COMPLETION.get(event["code"], event["code"])}')
    print(f'  no-op: {xhci.describe_event(event)}')

    print(f'--- waiting up to {args.wait:.0f}s for a device ---')
    deadline = time.monotonic() + args.wait
    attached = None
    while time.monotonic() < deadline:
        for e in xhci.poll_events():
            print(f'  event: {xhci.describe_event(e)}')
        for port in range(1, xhci.max_ports + 1):
            if xhci.portsc(port) & P_CCS:
                attached = port
                break
        if attached:
            break
        time.sleep(0.1)
    if not attached:
        print('  no device attached on any port')
        for port in range(1, xhci.max_ports + 1):
            print(xhci.port_report(port))
        return 1

    print(f'  device on port {attached}: {xhci.describe_port(xhci.portsc(attached))}')
    xhci.portsc_ack(attached, P_CSC)

    print(f'--- resetting port {attached} ---')
    value = xhci.reset_port(attached, warm=args.warm, timeout=2.0)
    print(f'  after reset: {xhci.describe_port(value)}')
    for e in xhci.drain(0.3):
        print(f'  event: {xhci.describe_event(e)}')
    xhci.portsc_ack(attached, P_PRC | P_WRC | P_CSC | P_PEC | P_PLC)

    value = xhci.portsc(attached)
    speed = (value >> 10) & 0xF
    print('--- result ---')
    print(xhci.port_report(attached))
    if value & P_PED:
        print(f'  PORT ENABLED at {SPEED_NAMES.get(speed, speed)} speed '
              f'(route: port {attached}, slot type from xCAP)')
    else:
        print('  port did not enable')
    print(f'  USBSTS {xhci.op_rd(USBSTS):#010x}')
    return 0 if value & P_PED else 1


CLASS_NAMES = {0x00: 'per-interface', 0x02: 'CDC control', 0x03: 'HID', 0x08: 'mass storage',
               0x09: 'hub', 0x0A: 'CDC data', 0x0E: 'video', 0xEF: 'miscellaneous',
               0xFF: 'vendor-specific'}


def describe_class(value):
    return CLASS_NAMES.get(value, f'{value:#04x}')


def walk_configuration(data):
    """Yield (type, bytes) for every descriptor in a configuration blob."""
    offset = 0
    while offset + 2 <= len(data):
        length = data[offset]
        if length < 2 or offset + length > len(data):
            break
        yield data[offset + 1], data[offset:offset + length]
        offset += length


def cmd_enumerate(args):
    """Bring the port up, address the device, and read its descriptors."""
    if args.base == USB0_BASE:
        print('REFUSED: USB0 carries this proxy console.', file=sys.stderr)
        return 1
    proxy = open_proxy(args)
    xhci = XHCI(proxy, args.base)

    print('--- init ---')
    xhci.init()
    for entry in xhci.protocols():
        print(f"  protocol {entry['name'].strip()} {entry['major']}.{entry['minor'] >> 4} "
              f"ports {entry['first']}..{entry['first'] + entry['count'] - 1} "
              f"slot type {entry['slot_type']}")

    print(f'--- waiting up to {args.wait:.0f}s for a device ---')
    deadline = time.monotonic() + args.wait
    port = None
    while time.monotonic() < deadline and port is None:
        for event in xhci.poll_events():
            print(f'  event: {xhci.describe_event(event)}')
        for candidate in range(1, xhci.max_ports + 1):
            if xhci.portsc(candidate) & P_CCS:
                port = candidate
                break
        if port is None:
            time.sleep(0.1)
    if port is None:
        print('  no device attached')
        return 1
    xhci.portsc_ack(port, P_CSC)

    print(f'--- resetting port {port} ---')
    xhci.reset_port(port, warm=(port == 2), timeout=2.0)
    xhci.portsc_ack(port, P_PRC | P_WRC | P_CSC | P_PEC | P_PLC)
    value = xhci.portsc(port)
    print(f'  {xhci.describe_port(value)}')
    if not value & P_PED:
        print('  port did not enable; cannot enumerate')
        return 1
    speed = (value >> 10) & 0xF
    xhci.drain(0.2)

    print('--- addressing ---')
    device = xhci.address_device(port, speed, xhci.slot_type_for_port(port))

    print('--- device descriptor ---')
    raw = xhci.get_descriptor(device, 1, 0, 18)
    if len(raw) < 18:
        print(f'  short device descriptor ({len(raw)} bytes): {raw.hex()}')
        return 1
    (length, kind, bcd_usb, dev_class, dev_sub, dev_proto, packet0,
     vendor, product, bcd_device, i_manufacturer, i_product, i_serial,
     configurations) = struct.unpack('<BBHBBBBHHHBBBB', raw)
    print(f'  USB {bcd_usb >> 8:x}.{(bcd_usb >> 4) & 0xF:x}  '
          f'VID:PID {vendor:04x}:{product:04x}  device {bcd_device >> 8:x}.{(bcd_device >> 4) & 0xF:x}')
    print(f'  class {describe_class(dev_class)} subclass {dev_sub:#04x} protocol {dev_proto:#04x}  '
          f'MPS0 {packet0}  configurations {configurations}')
    for label, index in (('manufacturer', i_manufacturer), ('product', i_product),
                         ('serial', i_serial)):
        try:
            text = xhci.get_string(device, index)
        except XHCIError as error:
            text = f'<{error}>'
        if text:
            print(f'  {label}: {text}')

    for number in range(configurations):
        print(f'--- configuration {number} ---')
        head = xhci.get_descriptor(device, 2, number, 9)
        if len(head) < 9:
            print(f'  short configuration descriptor: {head.hex()}')
            continue
        total = struct.unpack('<H', head[2:4])[0]
        full = xhci.get_descriptor(device, 2, number, min(total, 512))
        print(f'  {head[4]} interfaces, value {head[5]}, attributes {head[6]:#04x}, '
              f'{head[7] * 2} mA, {total} bytes')
        for kind, blob in walk_configuration(full):
            if kind == 4 and len(blob) >= 9:
                print(f'    interface {blob[2]} alt {blob[3]}: '
                      f'class {describe_class(blob[5])} subclass {blob[6]:#04x} '
                      f'protocol {blob[7]:#04x}, {blob[4]} endpoints')
            elif kind == 5 and len(blob) >= 7:
                address = blob[2]
                kinds = {0: 'control', 1: 'isochronous', 2: 'bulk', 3: 'interrupt'}
                mps = struct.unpack('<H', blob[4:6])[0]
                print(f'      endpoint {address & 0xF} {"IN" if address & 0x80 else "OUT"}: '
                      f'{kinds[blob[3] & 3]}, max packet {mps & 0x7FF}, interval {blob[6]}')
            elif kind == 0x24 and blob[2] == 0x0F and len(blob) >= 13:
                segment = struct.unpack('<H', blob[8:10])[0]
                try:
                    mac = xhci.get_string(device, blob[3])
                except XHCIError as error:
                    mac = f'<{error}>'
                pretty = ':'.join(mac[n:n + 2] for n in range(0, len(mac), 2)) if mac else '?'
                print(f'      CDC Ethernet: MAC {pretty}, max segment {segment} bytes')
            elif kind == 0x24 and blob[2] == 0x1A and len(blob) >= 6:
                ncm = struct.unpack('<H', blob[3:5])[0]   # bcdNcmVersion is 16-bit LE
                print(f'      CDC NCM {ncm >> 8:x}.{(ncm >> 4) & 0xF:x}, '
                      f'network capabilities {blob[5]:#04x}')
            elif kind == 0x24 and blob[2] == 0x06 and len(blob) >= 5:
                print(f'      CDC union: control interface {blob[3]}, data interface {blob[4]}')
            elif kind == 0x24 and len(blob) >= 3:
                print(f'      CDC functional descriptor subtype {blob[2]:#04x}: {blob.hex()}')
            elif kind == 11 and len(blob) >= 8:
                print(f'    interface association: first {blob[2]}, count {blob[3]}, '
                      f'class {describe_class(blob[4])} subclass {blob[5]:#04x} protocol {blob[6]:#04x}')

    print('--- done ---')
    print(f'  USBSTS {xhci.op_rd(USBSTS):#010x}, slot {device["slot"]}')
    return 0


def attach(proxy, base, wait=15.0, verbose=True, attempts=4):
    """init -> wait for a device -> reset its port -> address it.

    A device that was configured before the controller was reset under it can
    refuse the first SET_ADDRESS, so each retry escalates: plain reset, then a
    port disable to make the device see the link drop, then a longer settle.
    """
    xhci = XHCI(proxy, base, verbose=verbose)
    xhci.init()
    deadline = time.monotonic() + wait
    port = None
    while time.monotonic() < deadline and port is None:
        xhci.poll_events()
        for candidate in range(1, xhci.max_ports + 1):
            if xhci.portsc(candidate) & P_CCS:
                port = candidate
                break
        if port is None:
            time.sleep(0.1)
    if port is None:
        raise XHCIError('no device attached')

    last = None
    for attempt in range(attempts):
        if attempt:
            xhci.log(f'  retry {attempt}: {last}')
            if attempt >= 2:
                # Disable the port so the device sees the link go away.
                xhci.portsc_write(port, (xhci.portsc(port) & PORTSC_RW) | P_PED)
                time.sleep(0.3)
            time.sleep(0.2 * attempt)
        xhci.portsc_ack(port, P_CSC)
        xhci.reset_port(port, warm=(port == 2), timeout=2.0)
        xhci.portsc_ack(port, P_PRC | P_WRC | P_CSC | P_PEC | P_PLC)
        value = xhci.portsc(port)
        if not value & P_PED:
            last = f'port did not enable ({xhci.describe_port(value)})'
            continue
        time.sleep(0.1)          # USB reset recovery time before SET_ADDRESS
        xhci.drain(0.2)
        try:
            device = xhci.address_device(port, (value >> 10) & 0xF,
                                         xhci.slot_type_for_port(port))
            return xhci, device, port
        except XHCIError as error:
            last = str(error)
            xhci.log(f'  {error}; port now {xhci.describe_port(xhci.portsc(port))}')
    raise XHCIError(f'could not address the device after {attempts} attempts: {last}')


def parse_ntb(data):
    """Pull the Ethernet datagrams out of an NCM Transfer Block (NTB16)."""
    if len(data) < 12 or data[0:4] != b'NCMH':
        return None, []
    header_length, sequence, block_length, ndp_index = struct.unpack('<HHHH', data[4:12])
    frames = []
    seen = set()
    while ndp_index and ndp_index + 8 <= len(data) and ndp_index not in seen:
        seen.add(ndp_index)
        signature = data[ndp_index:ndp_index + 4]
        length, next_index = struct.unpack('<HH', data[ndp_index + 4:ndp_index + 8])
        offset = ndp_index + 8
        while offset + 4 <= ndp_index + length:
            index, size = struct.unpack('<HH', data[offset:offset + 4])
            if index == 0 and size == 0:
                break
            if index + size <= len(data):
                frames.append(data[index:index + size])
            offset += 4
        ndp_index = next_index
    return {'sequence': sequence, 'block_length': block_length,
            'header_length': header_length}, frames


ETHERTYPES = {0x0800: 'IPv4', 0x0806: 'ARP', 0x86DD: 'IPv6', 0x8100: 'VLAN'}


def build_ntb(datagrams, sequence):
    """Wrap Ethernet frames in an NTB16, the format the Mac advertises."""
    header = 12
    body = b''
    pointers = []
    offset = header
    for frame in datagrams:
        offset = (offset + 3) & ~3                     # wNdpOutAlignment is 4
        body += b'\0' * (offset - header - len(body))
        pointers.append((offset, len(frame)))
        body += frame
        offset += len(frame)
    ndp_index = (offset + 3) & ~3
    body += b'\0' * (ndp_index - header - len(body))
    ndp = b'NCM0' + struct.pack('<HH', 8 + 4 * (len(pointers) + 1), 0)
    for index, size in pointers:
        ndp += struct.pack('<HH', index, size)
    ndp += struct.pack('<HH', 0, 0)                    # terminating entry
    total = ndp_index + len(ndp)
    nth = b'NCMH' + struct.pack('<HHHH', header, sequence & 0xFFFF, total, ndp_index)
    return nth + body + ndp


def checksum16(data):
    if len(data) & 1:
        data += b'\0'
    total = sum(struct.unpack(f'>{len(data) // 2}H', data))
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return (~total) & 0xFFFF


def icmpv6(source, destination, body):
    """ICMPv6 with the IPv6 pseudo-header checksum filled in."""
    pseudo = source + destination + struct.pack('>IBBBB', len(body), 0, 0, 0, 58)
    payload = body[:2] + b'\0\0' + body[4:]
    value = checksum16(pseudo + payload)
    return body[:2] + struct.pack('>H', value) + body[4:]


def ipv6_frame(source_mac, destination_mac, source, destination, payload, hop_limit=255):
    header = struct.pack('>IHBB', 0x60000000, len(payload), 58, hop_limit) + source + destination
    return destination_mac + source_mac + b'\x86\xdd' + header + payload


def multicast_mac(address):
    return b'\x33\x33' + address[12:16]


def link_local(mac):
    """EUI-64 link-local address for a MAC, the usual fe80:: derivation."""
    eui = bytes([mac[0] ^ 0x02]) + mac[1:3] + b'\xff\xfe' + mac[3:6]
    return b'\xfe\x80' + b'\0' * 6 + eui


def format_ipv6(address):
    parts = [f'{(address[n] << 8) | address[n + 1]:x}' for n in range(0, 16, 2)]
    text = ':'.join(parts)
    for run in ('0:0:0:0:0:0:0', '0:0:0:0:0:0', '0:0:0:0:0', '0:0:0:0', '0:0:0', '0:0'):
        if f':{run}:' in text:
            return text.replace(f':{run}:', '::', 1)
    return text


def describe_frame(frame):
    if len(frame) < 14:
        return f'runt frame, {len(frame)} bytes'
    destination = ':'.join(f'{b:02x}' for b in frame[0:6])
    source = ':'.join(f'{b:02x}' for b in frame[6:12])
    kind = struct.unpack('>H', frame[12:14])[0]
    name = ETHERTYPES.get(kind, f'{kind:#06x}')
    return f'{source} -> {destination}  {name}, {len(frame)} bytes'


def cmd_ncm(args):
    """Configure the Mac's NCM function and open its bulk data path."""
    if args.base == USB0_BASE:
        print('REFUSED: USB0 carries this proxy console.', file=sys.stderr)
        return 1
    proxy = open_proxy(args)
    print('--- attach ---')
    xhci, device, port = attach(proxy, args.base, args.wait)

    control_interface = args.interface
    data_interface = control_interface + 1
    endpoint = (control_interface // 2) + 1          # function A -> ep 1, B -> ep 2
    dci_out, dci_in = endpoint * 2, endpoint * 2 + 1

    print(f'--- configuring function on interfaces {control_interface}/{data_interface} ---')
    xhci.set_configuration(device, 1)
    print('  SET_CONFIGURATION(1) accepted')

    xhci.configure_endpoints(device, [(dci_out, 2, 512), (dci_in, 6, 512)])

    xhci.set_interface(device, data_interface, 1)
    print(f'  SET_INTERFACE({data_interface}, alt 1) accepted')

    print('--- NCM parameters ---')
    try:
        raw = xhci.get_ntb_parameters(device, control_interface)
        if len(raw) >= 28:
            (length, formats, in_max, in_divisor, in_remainder, in_align, _reserved,
             out_max, out_divisor, out_remainder, out_align, out_datagrams) = \
                struct.unpack('<HHIHHHHIHHHH', raw[:28])
            print(f'  formats {formats:#06x}  NTB in max {in_max}  NTB out max {out_max}')
            print(f'  in: divisor {in_divisor} remainder {in_remainder} align {in_align}')
            print(f'  out: divisor {out_divisor} remainder {out_remainder} align {out_align}, '
                  f'max {out_datagrams} datagrams')
        else:
            print(f'  short response ({len(raw)} bytes): {raw.hex()}')
            in_max = 0x4000
    except XHCIError as error:
        print(f'  GET_NTB_PARAMETERS failed: {error}')
        in_max = 0x4000

    try:
        xhci.set_packet_filter(device, control_interface, 0x0F)
        print('  SET_ETHERNET_PACKET_FILTER(0x0f) accepted')
    except XHCIError as error:
        print(f'  SET_ETHERNET_PACKET_FILTER failed: {error}')

    print(f'--- listening on bulk IN (DCI {dci_in}) for {args.listen:.0f}s ---')
    buffer = xhci.free + 0x20000
    size = min(in_max or 0x4000, 0x8000)
    received = 0
    deadline = time.monotonic() + args.listen
    while time.monotonic() < deadline:
        try:
            data = xhci.transfer(device, dci_in, buffer, size,
                                 timeout=max(0.5, deadline - time.monotonic()))
        except XHCIError as error:
            print(f'  {error}')
            break
        if not data:
            continue
        header, frames = parse_ntb(data)
        if header is None:
            print(f'  {len(data)} bytes, not an NTB: {data[:32].hex()}')
            continue
        received += 1
        print(f'  NTB #{header["sequence"]}, {header["block_length"]} bytes, '
              f'{len(frames)} datagram(s)')
        for frame in frames:
            print(f'    {describe_frame(frame)}')
    if args.release:
        print('--- releasing the device ---')
        xhci.release(device, data_interface)
        xhci.stop()

    print('--- done ---')
    print(f'  {received} NTB(s) received; USBSTS {xhci.op_rd(USBSTS):#010x}; '
          f'link read retries {proxy.link.read_retries}')
    return 0 if received else 1


def cmd_link(args):
    """One session: configure NCM, learn the Mac's addresses, then talk to it.

    Everything happens in a single process on purpose. This Mac tolerates one
    host session per physical replug -- resetting the controller under a
    configured device leaves its XDCI refusing to chirp for high speed -- so
    attaching once and doing all the work here is the only workable shape.
    """
    if args.base == USB0_BASE:
        print('REFUSED: USB0 carries this proxy console.', file=sys.stderr)
        return 1
    proxy = open_proxy(args)
    print('--- attach ---')
    xhci, device, port = attach(proxy, args.base, args.wait)

    control_interface = args.interface
    data_interface = control_interface + 1
    endpoint = (control_interface // 2) + 1
    dci_out, dci_in = endpoint * 2, endpoint * 2 + 1

    xhci.set_configuration(device, 1)
    xhci.configure_endpoints(device, [(dci_out, 2, 512), (dci_in, 6, 512)])
    xhci.set_interface(device, data_interface, 1)
    raw = xhci.get_ntb_parameters(device, control_interface)
    in_max = struct.unpack('<I', raw[4:8])[0] if len(raw) >= 28 else 0x4000
    try:
        xhci.set_packet_filter(device, control_interface, 0x0F)
    except XHCIError as error:
        print(f'  packet filter: {error}')
    print(f'  NCM up on interfaces {control_interface}/{data_interface}, '
          f'endpoints DCI {dci_out}/{dci_in}, NTB in max {in_max}')

    # Our MAC is the one the function's Ethernet descriptor assigns the host.
    our_mac = bytes.fromhex(args.mac.replace(':', ''))
    our_address = link_local(our_mac)
    print(f'  q1n1 side: {args.mac}, {format_ipv6(our_address)}')

    in_buffer = xhci.free + 0x20000
    out_buffer = xhci.free + 0x30000
    size = min(in_max or 0x4000, 0x8000)
    sequence = 0

    def frames_in(timeout):
        data = xhci.receive(device, dci_in, in_buffer, size, timeout)
        if not data:
            return []
        header, frames = parse_ntb(data)
        return frames if header else []

    def send_frames(frames):
        nonlocal sequence
        xhci.send(device, dci_out, out_buffer, build_ntb(frames, sequence))
        sequence += 1

    print(f'--- learning the Mac from its own traffic ({args.learn:.0f}s) ---')
    peer_mac = peer_address = None
    deadline = time.monotonic() + args.learn
    while time.monotonic() < deadline and not (peer_mac and peer_address):
        for frame in frames_in(1.0):
            if len(frame) < 62 or frame[12:14] != b'\x86\xdd':
                continue
            source = frame[22:38]
            if source[0] == 0xFE and (source[1] & 0xC0) == 0x80:
                peer_mac, peer_address = frame[6:12], source
                break
    if not peer_address:
        print('  no IPv6 link-local traffic seen; cannot address the Mac')
        return 1
    pretty = ':'.join(f'{b:02x}' for b in peer_mac)
    print(f'  Mac side:  {pretty}, {format_ipv6(peer_address)}')

    print('--- announcing ourselves and pinging ---')
    # Unsolicited Neighbor Advertisement: seeds the Mac's neighbour cache so it
    # can answer without a solicitation round trip first.
    all_nodes = b'\xff\x02' + b'\0' * 13 + b'\x01'
    advert = icmpv6(our_address, all_nodes,
                    struct.pack('>BBHI', 136, 0, 0, 0x20000000) + our_address +
                    b'\x02\x01' + our_mac)
    send_frames([ipv6_frame(our_mac, multicast_mac(all_nodes), our_address, all_nodes, advert)])
    print('  sent unsolicited Neighbor Advertisement')

    identifier, replies = 0x71C1, 0
    for number in range(args.count):
        echo = icmpv6(our_address, peer_address,
                      struct.pack('>BBHHH', 128, 0, 0, identifier, number) + b'q1n1-xhci')
        send_frames([ipv6_frame(our_mac, peer_mac, our_address, peer_address, echo)])
        print(f'  echo request #{number} sent')

        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            got = frames_in(0.5)
            if not got:
                continue
            for frame in got:
                if len(frame) < 62 or frame[12:14] != b'\x86\xdd':
                    continue
                kind = frame[54]
                if kind == 135 and frame[62:78] == our_address:
                    # Neighbour Solicitation for us: answer it.
                    target = frame[22:38]
                    reply = icmpv6(our_address, target,
                                   struct.pack('>BBHI', 136, 0, 0, 0x60000000) +
                                   our_address + b'\x02\x01' + our_mac)
                    send_frames([ipv6_frame(our_mac, frame[6:12], our_address, target, reply)])
                    print('    answered a Neighbor Solicitation')
                elif kind == 129 and frame[22:38] == peer_address:
                    sequence_number = struct.unpack('>H', frame[60:62])[0]
                    print(f'    ECHO REPLY #{sequence_number} from '
                          f'{format_ipv6(peer_address)}, {len(frame)} bytes')
                    replies += 1
                    deadline = 0
                    break
            if not deadline:
                break

    print('--- releasing the device ---')
    xhci.release(device, data_interface)
    xhci.stop()
    print('--- done ---')
    print(f'  {replies}/{args.count} echo replies; link read retries {proxy.link.read_retries}')
    return 0 if replies else 1


def cmd_watch(args):
    """Poll the ports and the event ring, printing changes as they happen."""
    proxy = open_proxy(args)
    xhci = XHCI(proxy, args.base, verbose=False)
    if xhci.halted():
        print('controller is halted; run `init` first', file=sys.stderr)
        return 1
    previous = {port: None for port in range(1, xhci.max_ports + 1)}
    deadline = time.monotonic() + args.seconds
    while time.monotonic() < deadline:
        for event in xhci.poll_events():
            print(f'{time.strftime("%H:%M:%S")} event: {xhci.describe_event(event)}')
        for port in previous:
            value = xhci.portsc(port)
            if value != previous[port]:
                print(f'{time.strftime("%H:%M:%S")} port {port}: {xhci.describe_port(value)}')
                previous[port] = value
        time.sleep(args.interval)
    return 0


def cmd_stop(args):
    if args.base == USB0_BASE:
        print('REFUSED: USB0 carries this proxy console.', file=sys.stderr)
        return 1
    proxy = open_proxy(args)
    xhci = XHCI(proxy, args.base)
    xhci.stop()
    print(f'halted: USBSTS {xhci.op_rd(USBSTS):#010x}')


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--device', help='proxy serial device')
    parser.add_argument('--debug', action='store_true')
    parser.add_argument('--base', type=lambda v: int(v, 0), default=USB1_BASE,
                        help=f'controller base (default USB1 {USB1_BASE:#x})')
    sub = parser.add_subparsers(dest='command', required=True)

    sub.add_parser('survey', help='report both controllers without touching them')
    init = sub.add_parser('init', help='reset, configure rings, run, and report the ports')
    init.add_argument('--reset-ports', type=int, nargs='*', default=None,
                      help='ports to reset after starting (2 uses a warm reset)')
    bring = sub.add_parser('bringup', help='init, wait for a device, and reset its port')
    bring.add_argument('--wait', type=float, default=15.0, help='seconds to wait for a device')
    bring.add_argument('--warm', action='store_true', help='use a warm reset (USB3 ports)')
    enum = sub.add_parser('enumerate', help='bring up the port and read the device descriptors')
    enum.add_argument('--wait', type=float, default=15.0)
    ncm = sub.add_parser('ncm', help='configure the NCM function and read its bulk IN endpoint')
    ncm.add_argument('--wait', type=float, default=15.0)
    ncm.add_argument('--listen', type=float, default=10.0)
    ncm.add_argument('--interface', type=int, default=0,
                     help='NCM control interface: 0 (function A) or 2 (function B)')
    ncm.add_argument('--no-release', dest='release', action='store_false',
                     help='leave the device configured instead of unwinding it')
    ncm.set_defaults(release=True)
    link = sub.add_parser('link', help='configure NCM and exchange IPv6 traffic with the Mac')
    link.add_argument('--wait', type=float, default=15.0)
    link.add_argument('--learn', type=float, default=15.0)
    link.add_argument('--count', type=int, default=3)
    link.add_argument('--timeout', type=float, default=3.0)
    link.add_argument('--interface', type=int, default=0)
    link.add_argument('--mac', default='12:77:60:eb:84:a7',
                      help="the host MAC from the function's Ethernet descriptor")
    watch = sub.add_parser('watch', help='poll ports and events, printing changes')
    watch.add_argument('--seconds', type=float, default=30.0)
    watch.add_argument('--interval', type=float, default=0.2)
    sub.add_parser('stop', help='halt the controller')

    args = parser.parse_args()
    return {'survey': cmd_survey, 'init': cmd_init, 'bringup': cmd_bringup,
            'enumerate': cmd_enumerate, 'ncm': cmd_ncm, 'link': cmd_link,
            'watch': cmd_watch, 'stop': cmd_stop}[args.command](args)


if __name__ == '__main__':
    sys.exit(main() or 0)

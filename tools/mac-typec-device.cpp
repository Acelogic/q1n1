/* SPDX-License-Identifier: MIT */
/* Request Mac device data role on one explicitly selected, currently attached
 * AppleHPM controller. Selection uses an IORegistry ID from this Mac boot, not
 * an enumeration index. Default is read-only. No unlock or register writes.
 *
 * clang++ -std=c++14 -Wall -Wextra -o build/mac-typec-device \
 *   tools/mac-typec-device.cpp -I../../aurora-silicon/macvdmtool \
 *   -framework CoreFoundation -framework IOKit
 * sudo build/mac-typec-device <registry-id> [--device]
 *
 * AppleHPMLib.h is from the separately licensed macvdmtool. SWUF is the TI
 * TPS6598x data-role request also used by Linux drivers/usb/typec/tipd/core.c.
 * Apple's implementation is verified by reading back the role, not inferred
 * from the task return code. Power-role policy is not changed by this tool.
 */
#include <CoreFoundation/CoreFoundation.h>
#include <IOKit/IOKitLib.h>
#include <IOKit/IOCFPlugIn.h>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cerrno>
#include <unistd.h>
#include "AppleHPMLib.h"

struct Interface {
    IOCFPlugInInterface **plugin = nullptr;
    AppleHPMLib **device = nullptr;
    ~Interface() {
        if (device) (*device)->Release(device);
        if (plugin) IODestroyPlugInInterface(plugin);
    }
    bool read(uint8_t reg, uint8_t *data, uint64_t expected) {
        uint8_t bytes[64] = {};
        uint64_t length = 0;
        IOReturn rc = (*device)->Read(device, 0, reg, bytes, sizeof(bytes), 0, &length);
        if (rc || length != expected) {
            std::fprintf(stderr, "Register 0x%02x: status 0x%x, length %llu (expected %llu)\n",
                         reg, rc, (unsigned long long)length, (unsigned long long)expected);
            return false;
        }
        std::memcpy(data, bytes, (size_t)length);
        return true;
    }
    bool status(uint32_t *out) {
        uint8_t b[4];
        if (!read(0x1a, b, sizeof(b))) return false;
        *out = uint32_t(b[0]) | uint32_t(b[1]) << 8 | uint32_t(b[2]) << 16 | uint32_t(b[3]) << 24;
        return true;
    }
};

int main(int argc, char **argv)
{
    bool active = argc == 3 && !std::strcmp(argv[2], "--device");
    if (argc != 2 && !active) {
        std::fprintf(stderr, "Usage: %s <current-boot-registry-id> [--device]\n", argv[0]);
        return 2;
    }
    char *end = nullptr;
    errno = 0;
    uint64_t wanted = std::strtoull(argv[1], &end, 0);
    if (errno || !end || end == argv[1] || *end || !wanted || argv[1][0] == '-') return 2;
    io_iterator_t iter = 0;
    if (IOServiceGetMatchingServices(kIOMainPortDefault, IOServiceMatching("AppleHPM"), &iter)) return 1;
    io_service_t service, selected = 0;
    while ((service = IOIteratorNext(iter))) {
        uint64_t id = 0;
        if (!IORegistryEntryGetRegistryEntryID(service, &id) && id == wanted) selected = service;
        else IOObjectRelease(service);
    }
    IOObjectRelease(iter);
    if (!selected) {
        std::fprintf(stderr, "REFUSED: no AppleHPM with the specified current-boot registry ID.\n");
        return 1;
    }
    io_string_t path = {};
    IORegistryEntryGetPath(selected, kIOServicePlane, path);
    std::printf("Selected 0x%llx: %s\n", (unsigned long long)wanted, path);
    Interface hpm;
    SInt32 score = 0;
    IOReturn rc = IOCreatePlugInInterfaceForService(selected, kAppleHPMLibType,
                        kIOCFPlugInInterfaceID, &hpm.plugin, &score);
    IOObjectRelease(selected);
    if (rc || !hpm.plugin) {
        std::fprintf(stderr, "AppleHPM access unavailable: 0x%x (administrator access required).\n", rc);
        return 1;
    }
    if ((*hpm.plugin)->QueryInterface(hpm.plugin, CFUUIDGetUUIDBytes(kAppleHPMLibInterface),
                                     (LPVOID *)&hpm.device) || !hpm.device) return 1;
    uint8_t mode[4], power[6];
    uint32_t before = 0;
    if (!hpm.read(0x03, mode, sizeof(mode)) || std::memcmp(mode, "APP ", 4) ||
        !hpm.status(&before) || !hpm.read(0x3f, power, sizeof(power))) return 1;
    std::printf("Before: status 0x%08x, power-path 0x%02x, data role %s.\n",
                before, power[0], before & (1u << 6) ? "host/DFP" : "device/UFP");
    if (!(before & 1) || !(power[0] & 1)) {
        std::fprintf(stderr, "REFUSED: selected port is not attached.\n"); return 1;
    }
    if (!(before & (1u << 6))) {
        std::puts("Already device/UFP; no command sent."); return 0;
    }
    if (!active) { std::puts("Read-only preflight complete; no command sent."); return 0; }
    std::puts("Sending one SWUF request. No unlock or retries.");
    std::fflush(stdout);
    rc = (*hpm.device)->Command(hpm.device, 0, 0x53575546, 0);
    std::printf("SWUF API status: 0x%x\n", rc);
    uint8_t task[64] = {};
    uint64_t length = 0;
    IOReturn read_rc = (*hpm.device)->Read(hpm.device, 0, 9, task, sizeof(task), 0, &length);
    if (!read_rc && length && length <= sizeof(task)) std::printf("Task result first byte: 0x%02x\n", task[0]);
    uint32_t after = before;
    for (unsigned n = 0; n < 30; n++) {
        usleep(100000);
        if (!hpm.status(&after)) return 1;
        if (!(after & 1) || !(after & (1u << 6))) break;
    }
    bool changed = (after & 1) && !(after & (1u << 6));
    std::printf("After: status 0x%08x; device/UFP %s; power-role bit %s.\n", after,
                changed ? "confirmed" : "not confirmed", (after ^ before) & (1u << 5) ? "changed" : "unchanged");
    return !rc && changed ? 0 : 1;
}

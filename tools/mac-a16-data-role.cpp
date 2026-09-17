/* SPDX-License-Identifier: MIT */
/* One-boot, one-port experiment for this Mac/A16 connection. Default is read-only.
 * --host sends SWDF once, with no unlock, debug mode, power swap or register write.
 * The current boot's HPM registry identity deliberately prevents reuse after reboot.
 * Protocol declaration: separately licensed macvdmtool/AppleHPMLib.h.
 * SWDF: TI SLVA843A and Linux drivers/usb/typec/tipd/core.c.
 */
#include <CoreFoundation/CoreFoundation.h>
#include <IOKit/IOKitLib.h>
#include <IOKit/IOCFPlugIn.h>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <unistd.h>
#include "AppleHPMLib.h"

static constexpr uint64_t expected_registry_id = 0x10000060d;
static constexpr uint32_t swap_dfp = 0x53574446; /* AppleHPMLib Command uses 'SWDF'. */

struct Interface {
    IOCFPlugInInterface **plugin = nullptr;
    AppleHPMLib **device = nullptr;
    ~Interface() {
        if (device) (*device)->Release(device);
        if (plugin) IODestroyPlugInInterface(plugin);
    }
    bool read(uint8_t reg, uint8_t *data, uint64_t expected_size) {
        uint8_t bytes[64] = {};
        uint64_t size = 0;
        IOReturn rc = (*device)->Read(device, 0, reg, bytes, sizeof(bytes), 0, &size);
        if (rc || size != expected_size) {
            std::printf("Register 0x%02x unavailable: status 0x%x, length %llu\n",
                        reg,rc,(unsigned long long)size);
            return false;
        }
        std::memcpy(data,bytes,(size_t)size);
        return true;
    }
};
static uint32_t little32(const uint8_t *b)
{
    return b[0] | (uint32_t(b[1])<<8) | (uint32_t(b[2])<<16) | (uint32_t(b[3])<<24);
}
static bool target(io_service_t service)
{
    uint64_t id = 0;
    if (IORegistryEntryGetRegistryEntryID(service,&id) || id != expected_registry_id) return false;
    auto rid = IORegistryEntryCreateCFProperty(service,CFSTR("RID"),kCFAllocatorDefault,0);
    int32_t value = -1;
    if (rid && CFGetTypeID(rid)==CFNumberGetTypeID()) CFNumberGetValue((CFNumberRef)rid,kCFNumberSInt32Type,&value);
    if (rid) CFRelease(rid);
    if (value != 1) return false;
    auto type = IORegistryEntryCreateCFProperty(service,CFSTR("IONameMatched"),kCFAllocatorDefault,0);
    bool matched = type && CFGetTypeID(type)==CFStringGetTypeID() && CFEqual(type,CFSTR("usbc,sn201202x,spmi"));
    if (type) CFRelease(type);
    if (!matched) return false;
    io_registry_entry_t parent = 0;
    if (IORegistryEntryGetParentEntry(service,kIOServicePlane,&parent)) return false;
    auto location = IORegistryEntryCreateCFProperty(parent,CFSTR("port-location"),kCFAllocatorDefault,0);
    static const char name[]="left-front";
    matched = location && CFGetTypeID(location)==CFDataGetTypeID() &&
              CFDataGetLength((CFDataRef)location)==sizeof(name) &&
              !std::memcmp(CFDataGetBytePtr((CFDataRef)location),name,sizeof(name));
    if (location) CFRelease(location);
    IOObjectRelease(parent);
    return matched;
}
int main(int argc, char **argv)
{
    bool active = argc==2 && !std::strcmp(argv[1],"--host");
    if (argc!=1 && !active) {
        std::fprintf(stderr,"Usage: %s [--host]\nDefault: read-only preflight. --host requests SWDF once on verified HPM1.\n",argv[0]);
        return 2;
    }
    io_iterator_t iter=0;
    if (IOServiceGetMatchingServices(kIOMainPortDefault,IOServiceMatching("AppleHPM"),&iter)) return 1;
    io_service_t service, selected=0;
    unsigned matches=0;
    while ((service=IOIteratorNext(iter))) {
        if (target(service)) { matches++; if (!selected) { selected=service; continue; } }
        IOObjectRelease(service);
    }
    IOObjectRelease(iter);
    if (matches!=1) {
        if (selected) IOObjectRelease(selected);
        std::fprintf(stderr,"REFUSED: current-boot HPM1/left-front identity mismatch.\n");return 1;
    }
    Interface hpm;
    SInt32 score=0;
    IOReturn rc=IOCreatePlugInInterfaceForService(selected,kAppleHPMLibType,kIOCFPlugInInterfaceID,&hpm.plugin,&score);
    IOObjectRelease(selected);
    if (rc || !hpm.plugin) { std::fprintf(stderr,"Plugin unavailable: 0x%x\n",rc);return 1; }
    if ((*hpm.plugin)->QueryInterface(hpm.plugin,CFUUIDGetUUIDBytes(kAppleHPMLibInterface),(LPVOID *)&hpm.device) || !hpm.device) return 1;
    uint8_t mode[4],status_bytes[4],power[6];
    if (!hpm.read(0x03,mode,sizeof(mode)) || std::memcmp(mode,"APP ",4) ||
        !hpm.read(0x1a,status_bytes,sizeof(status_bytes)) || !hpm.read(0x3f,power,sizeof(power))) {
        std::fprintf(stderr,"REFUSED: firmware mode or status format mismatch.\n");return 1;
    }
    uint32_t status=little32(status_bytes);
    std::printf("Matched current-boot HPM1, left-front. Status 0x%08x; power 0x%02x.\n",status,power[0]);
    if (!(status&1) || !(power[0]&1) || (power[0]&0xc)!=0xc || (power[0]&2)) {
        std::fprintf(stderr,"REFUSED: expected attached PD source-power link.\n");return 1;
    }
    if (status&(1u<<6)) { std::puts("Mac already reports DFP/host; no command sent.");return 0; }
    if (!active) { std::puts("Preflight passed: UFP/peripheral. No command sent.");return 0; }
    std::puts("Requesting SWDF once on HPM1: Mac USB host data role.");
    std::fflush(stdout);
    rc=(*hpm.device)->Command(hpm.device,0,swap_dfp,0);
    std::printf("SWDF API result: 0x%x\n",rc);
    /* Read the result without assuming the undocumented status nibble implies success. */
    uint8_t result[64]={};uint64_t length=0;
    IOReturn read_rc=(*hpm.device)->Read(hpm.device,0,0x09,result,sizeof(result),0,&length);
    std::printf("Task-result read: 0x%x, length %llu",read_rc,(unsigned long long)length);
    if (!read_rc && length && length<=sizeof(result)) std::printf(", first byte 0x%02x",result[0]);
    std::puts("");
    bool host=false;
    for (unsigned n=0;n<20;n++) {
        usleep(100000);
        if (!hpm.read(0x1a,status_bytes,sizeof(status_bytes))) break;
        status=little32(status_bytes);
        if (!(status&1)) break;
        if (status&(1u<<6)) { host=true;break; }
    }
    std::printf("After request: status 0x%08x; DFP/host %s. No retries.\n",status,host?"yes":"not confirmed");
    return !rc && host ? 0 : 1;
}

/* SPDX-License-Identifier: Apache-2.0 */
/* Read, and optionally swap, the data role of this Mac's USB-C ports.
 *
 * The A16's PD firmware refuses to become a UFP: UCSI reports
 * GET_CONNECTOR_CAPABILITY swap-to-UFP = 0, rejects SET_UOR and does not
 * implement SET_CCOM (see q1n1/docs/A16-Q1N1-PROXY.md). An incoming PD data
 * role swap is a different path through that firmware than an outgoing OPM
 * request, so this asks from the Mac side instead: 'SWDF' tells the local ACE
 * port controller to request DFP, which would leave the A16 as the device.
 *
 * The HPM plumbing (plug-in interface, register and 4CC command access, the
 * unlock key derived from the Mac model) is taken from macvdmtool, which is
 * Apache-2.0 and lives in ../../aurora-silicon/macvdmtool. Nothing there is
 * modified; this is a separate tool so the Aurora targets stay untouched.
 *
 * Build:
 *   clang++ -std=c++14 -Wall -Wextra -o build/mac-typec-role \
 *     tools/mac-typec-role.cpp -I../aurora-silicon/macvdmtool \
 *     -framework CoreFoundation -framework IOKit
 *
 * Usage:
 *   mac-typec-role scan             read every port, change nothing
 *   mac-typec-role swap <chip>      request DFP on one port (4CC 'SWDF')
 *   mac-typec-role command <chip> <4cc>
 */
#include "AppleHPMLib.h"

#include <cstdio>
#include <cstring>
#include <stdexcept>
#include <string>
#include <vector>

#include <CoreFoundation/CoreFoundation.h>
#include <IOKit/IOKitLib.h>

struct failure : public std::runtime_error {
    failure(const char *what) : std::runtime_error(what) {}
};

struct Port {
    io_service_t service;
    IOCFPlugInInterface **plugin = nullptr;
    AppleHPMLib **device = nullptr;

    Port(io_service_t service) : service(service)
    {
        SInt32 score;
        if (IOCreatePlugInInterfaceForService(service, kAppleHPMLibType, kIOCFPlugInInterfaceID,
                                              &plugin, &score) != kIOReturnSuccess)
            throw failure("IOCreatePlugInInterfaceForService failed (needs root?)");
        if ((*plugin)->QueryInterface(plugin, CFUUIDGetUUIDBytes(kAppleHPMLibInterface),
                                      (LPVOID *)&device) != S_OK)
            throw failure("QueryInterface failed");
    }
    ~Port()
    {
        if (plugin)
            IODestroyPlugInInterface(plugin);
    }

    std::string read(uint64_t chip, uint8_t address, size_t length = 64)
    {
        std::string value;
        value.resize(length);
        uint64_t got = 0;
        if ((*device)->Read(device, chip, address, &value[0], length, 0, &got) != kIOReturnSuccess)
            throw failure("register read failed");
        value.resize(got ? got : length);
        return value;
    }
    int command(uint64_t chip, uint32_t code, std::string args = "")
    {
        if (args.length())
            (*device)->Write(device, chip, 9, args.data(), args.length(), 0);
        if ((*device)->Command(device, chip, code, 0))
            return -1;
        std::string result = this->read(chip, 9, 8);
        return result[0] & 0xf;
    }
};

static std::string hex(const std::string &data, size_t limit)
{
    std::string out;
    char byte[4];
    for (size_t n = 0; n < data.size() && n < limit; n++) {
        snprintf(byte, sizeof(byte), "%02x", (uint8_t)data[n]);
        out += byte;
    }
    return out;
}

static std::string text(const std::string &data)
{
    std::string out;
    for (char c : data) {
        if (!c)
            break;
        out += (c >= 32 && c < 127) ? c : '.';
    }
    return out;
}

/* macvdmtool derives the ACE unlock key from the Mac model name. */
static uint32_t unlock_key()
{
    io_service_t service =
        IOServiceGetMatchingService(kIOMainPortDefault, IOServiceMatching("IOPlatformExpertDevice"));
    if (!service)
        throw failure("IOPlatformExpertDevice not found");
    io_name_t name;
    if (IORegistryEntryGetName(service, name) != kIOReturnSuccess)
        throw failure("IORegistryEntryGetName failed");
    IOObjectRelease(service);
    printf("Mac type: %s\n", name);
    return ((uint32_t)name[0] << 24) | ((uint32_t)name[1] << 16) | ((uint32_t)name[2] << 8) | name[3];
}

static std::vector<io_service_t> find_controllers()
{
    io_iterator_t iterator = 0;
    if (IOServiceGetMatchingServices(kIOMainPortDefault, IOServiceMatching("AppleHPM"), &iterator) !=
        kIOReturnSuccess)
        throw failure("IOServiceGetMatchingServices failed");
    std::vector<io_service_t> found;
    io_service_t service;
    while ((service = IOIteratorNext(iterator)))
        found.push_back(service);
    IOObjectRelease(iterator);
    return found;
}

/* Registers follow the TPS6598x map that Apple's ACE is based on:
 * 0x03 mode (4CC), 0x1A status, 0x3F power path status. Bit meanings vary by
 * revision, so the raw bytes are printed too: with a dock on one port and the
 * A16 on another, the differing bits identify themselves. */
static void report(Port &port, uint64_t chip)
{
    std::string mode, status, power;
    try {
        mode = port.read(chip, 0x03, 8);
        status = port.read(chip, 0x1a, 8);
        power = port.read(chip, 0x3f, 8);
    } catch (failure &) {
        return; /* no such chip address on this controller */
    }
    uint8_t path = (uint8_t)power[0];
    uint64_t state = 0;
    memcpy(&state, status.data(), sizeof(state) <= status.size() ? sizeof(state) : status.size());
    const char *connection = (path & 1) ? ((path & 2) ? "sink (partner supplies power)" : "source")
                                        : "nothing attached";
    printf("  chip %llu  mode %-6s  connection: %s\n", (unsigned long long)chip,
           text(mode).c_str(), connection);
    printf("      status 0x%s  power-path 0x%s\n", hex(status, 8).c_str(), hex(power, 5).c_str());
    printf("      plug-present %llu  port-role %s  data-role %s  orientation %llu\n",
           (unsigned long long)(state & 1), (state >> 5) & 1 ? "source" : "sink",
           (state >> 6) & 1 ? "DFP (this Mac is the host)" : "UFP (this Mac is the device)",
           (unsigned long long)((state >> 4) & 1));
}

int main(int argc, char **argv)
{
    std::string action = argc > 1 ? argv[1] : "scan";
    try {
        std::vector<io_service_t> controllers = find_controllers();
        if (controllers.empty())
            throw failure("no AppleHPM controllers (run with sudo?)");
        printf("Found %zu AppleHPM controller(s)\n", controllers.size());

        if (action == "scan") {
            for (size_t n = 0; n < controllers.size(); n++) {
                io_name_t name = {0};
                IORegistryEntryGetName(controllers[n], name);
                printf("controller %zu (%s)\n", n, name);
                Port port(controllers[n]);
                for (uint64_t chip = 0; chip < 2; chip++)
                    report(port, chip);
            }
            return 0;
        }

        if (action == "dump" && argc >= 3) {
            /* Read-only sweep so two ports can be compared: one Mac-as-host,
             * one Mac-as-device. Whatever differs is where the role lives. */
            size_t index = (size_t)strtoul(argv[2], nullptr, 0);
            if (index >= controllers.size())
                throw failure("controller index out of range");
            Port port(controllers[index]);
            for (unsigned reg = 0x00; reg <= 0x7f; reg++) {
                std::string value;
                try {
                    value = port.read(0, (uint8_t)reg, 16);
                } catch (failure &) {
                    continue;
                }
                bool empty = true;
                for (char c : value)
                    if (c) empty = false;
                if (empty)
                    continue;
                printf("%02x %s\n", reg, hex(value, 16).c_str());
            }
            return 0;
        }

        if ((action == "swap" || action == "command") && argc >= 3) {
            size_t index = (size_t)strtoul(argv[2], nullptr, 0);
            if (index >= controllers.size())
                throw failure("controller index out of range");
            uint64_t chip = argc >= 4 && action == "swap" ? strtoull(argv[3], nullptr, 0) : 0;
            uint32_t code = 'SWDF';
            if (action == "command") {
                if (argc < 4 || strlen(argv[3]) != 4)
                    throw failure("give a four-character command, e.g. SWDF");
                code = ((uint32_t)argv[3][0] << 24) | ((uint32_t)argv[3][1] << 16) |
                       ((uint32_t)argv[3][2] << 8) | (uint32_t)argv[3][3];
                chip = argc >= 5 ? strtoull(argv[4], nullptr, 0) : 0;
            }
            Port port(controllers[index]);
            printf("before:\n");
            report(port, chip);
            int result = port.command(chip, code);
            printf("command '%c%c%c%c' on controller %zu chip %llu -> %d%s\n", (code >> 24) & 0xff,
                   (code >> 16) & 0xff, (code >> 8) & 0xff, code & 0xff, index,
                   (unsigned long long)chip, result,
                   result == 0 ? " (accepted)" : result < 0 ? " (not executed)" : " (rejected)");
            if (result != 0) {
                printf("retrying after unlocking the ACE\n");
                uint32_t key = unlock_key();
                std::string args((const char *)&key, sizeof(key));
                printf("  LOCK -> %d\n", port.command(chip, 'LOCK', args));
                result = port.command(chip, code);
                printf("  retry -> %d\n", result);
            }
            printf("after:\n");
            report(port, chip);
            return result == 0 ? 0 : 1;
        }

        printf("Usage: %s scan | swap <controller> [chip] | command <controller> <4CC> [chip]\n",
               argv[0]);
        return 1;
    } catch (failure &problem) {
        printf("error: %s\n", problem.what());
        return 2;
    }
}

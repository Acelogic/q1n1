# Invoke from the repository root: make -f platform/uefi/Makefile
LLVM ?= /opt/homebrew/opt/llvm/bin
CC := $(if $(wildcard $(LLVM)/clang),$(LLVM)/clang,clang)
LD := lld-link
OUT ?= build/uefi
# -MMD -MP is not a nicety here. These objects share structs that live in shared
# memory across a chainload, so when a header changes and only some objects are
# rebuilt, the survivors disagree about where the fields are -- and the failure
# is silent: counters read as pointers, a stage adopts a block it cannot parse,
# and the corruption looks like a driver bug. That cost two separate debugging
# sessions before anyone suspected the build.
CFLAGS := --target=aarch64-pc-windows-msvc -O2 -ffreestanding -fno-builtin \
          -fno-stack-protector -fshort-wchar -mgeneral-regs-only -mno-outline-atomics \
          -Wall -Wextra -Werror -Iplatform/uefi -MMD -MP

LLD := $(if $(wildcard $(LLVM)/lld),$(LLVM)/lld -flavor gnu,ld.lld)
OBJCOPY := $(if $(wildcard $(LLVM)/llvm-objcopy),$(LLVM)/llvm-objcopy,llvm-objcopy)
# A chainloaded stage is a flat ELF-linked binary, not a PE image: no firmware
# loads it, the running payload uploads it over the proxy and jumps to it.
STAGE_CFLAGS := --target=aarch64-unknown-none-elf -O2 -ffreestanding -fno-builtin \
    -fno-stack-protector -mgeneral-regs-only -mno-outline-atomics -fno-pic -mcmodel=small \
    -Wall -Wextra -Werror -Iplatform/uefi -DQ1N1_STAGE -MMD -MP
STAGE_OBJS := $(addprefix $(OUT)/stage/,main.obj entry.obj q1n1-proxy.obj proxy-asm.obj \
    qcom-dwc3.obj gicv3.obj console.obj sysinfo.obj fbcon.obj xhci.obj ncm.obj ncm-proxy.obj)

.PHONY: all clean usb-probe usb-role-probe usb-role-fixture usb-role-change usb-serial usb-typec-probe usb-ucsi-probe
all: $(OUT)/q1n1.efi stage
stage: $(OUT)/q1n1-stage.bin $(OUT)/q1n1-stage-alt.bin $(OUT)/q1n1-stage-qemu.bin $(OUT)/q1n1-stage-qemu-alt.bin
.PHONY: stage
usb-probe: $(OUT)/q1n1-usb-probe.efi
usb-role-probe: $(OUT)/q1n1-usb-role-probe.efi
usb-role-fixture: $(OUT)/usb-role-fixture.efi
usb-role-change: $(OUT)/q1n1-usb-role-change.efi
usb-controller-probe: $(OUT)/q1n1-usb-controller-probe.efi
.PHONY: usb-controller-probe
usb-serial: $(OUT)/q1n1-usb-serial.efi
usb-serial-v3: $(OUT)/q1n1-usb-serial-v3.efi
.PHONY: usb-serial-v3
usb-ebs-prep: $(OUT)/q1n1-usb-ebs-prep.efi
usb-ebs: $(OUT)/q1n1-usb-ebs.efi
.PHONY: usb-ebs-prep usb-ebs
usb-typec-probe: $(OUT)/q1n1-usb-typec-probe.efi
usb-ucsi-probe: $(OUT)/q1n1-usb-ucsi-probe.efi
usb-usbc-probe: $(OUT)/q1n1-usb-usbc-probe.efi
.PHONY: usb-usbc-probe
usb-ucsi-reg-probe: $(OUT)/q1n1-usb-ucsi-reg-probe.efi
.PHONY: usb-ucsi-reg-probe
usb-ucsi-status: $(OUT)/q1n1-usb-ucsi-status.efi
.PHONY: usb-ucsi-status
usb-ucsi-status-v2: $(OUT)/q1n1-usb-ucsi-status-v2.efi
.PHONY: usb-ucsi-status-v2
usb-ucsi-device0: $(OUT)/q1n1-usb-ucsi-device0.efi
.PHONY: usb-ucsi-device0

$(OUT)/usb-ucsi-device0.obj: platform/uefi/usb-ucsi-device0.c platform/uefi/usb-ucsi-status-v2.c platform/uefi/usb-ucsi-status.c platform/uefi/usb-ucsi-reg-probe.c platform/uefi/efi.h
	@mkdir -p $(OUT)
	$(CC) $(CFLAGS) -c $< -o $@

$(OUT)/q1n1-usb-ucsi-device0.efi: $(OUT)/usb-ucsi-device0.obj
	$(LD) /subsystem:efi_application /entry:efi_main /nodefaultlib /machine:arm64 \
	    /dynamicbase /out:$@ $^

$(OUT)/usb-ucsi-status-v2.obj: platform/uefi/usb-ucsi-status-v2.c platform/uefi/usb-ucsi-status.c platform/uefi/usb-ucsi-reg-probe.c platform/uefi/efi.h
	@mkdir -p $(OUT)
	$(CC) $(CFLAGS) -c $< -o $@

$(OUT)/q1n1-usb-ucsi-status-v2.efi: $(OUT)/usb-ucsi-status-v2.obj
	$(LD) /subsystem:efi_application /entry:efi_main /nodefaultlib /machine:arm64 \
	    /dynamicbase /out:$@ $^

$(OUT)/usb-ucsi-status.obj: platform/uefi/usb-ucsi-status.c platform/uefi/usb-ucsi-reg-probe.c platform/uefi/efi.h
	@mkdir -p $(OUT)
	$(CC) $(CFLAGS) -c $< -o $@

$(OUT)/q1n1-usb-ucsi-status.efi: $(OUT)/usb-ucsi-status.obj
	$(LD) /subsystem:efi_application /entry:efi_main /nodefaultlib /machine:arm64 \
	    /dynamicbase /out:$@ $^

$(OUT)/usb-ucsi-reg-probe.obj: platform/uefi/usb-ucsi-reg-probe.c platform/uefi/efi.h
	@mkdir -p $(OUT)
	$(CC) $(CFLAGS) -c $< -o $@

$(OUT)/q1n1-usb-ucsi-reg-probe.efi: $(OUT)/usb-ucsi-reg-probe.obj
	$(LD) /subsystem:efi_application /entry:efi_main /nodefaultlib /machine:arm64 \
	    /dynamicbase /out:$@ $^

$(OUT)/usb-usbc-probe.obj: platform/uefi/usb-usbc-probe.c platform/uefi/usb-ucsi-probe.c platform/uefi/efi.h
	@mkdir -p $(OUT)
	$(CC) $(CFLAGS) -c $< -o $@

$(OUT)/q1n1-usb-usbc-probe.efi: $(OUT)/usb-usbc-probe.obj
	$(LD) /subsystem:efi_application /entry:efi_main /nodefaultlib /machine:arm64 \
	    /dynamicbase /out:$@ $^

$(OUT)/usb-ucsi-probe.obj: platform/uefi/usb-ucsi-probe.c platform/uefi/efi.h
	@mkdir -p $(OUT)
	$(CC) $(CFLAGS) -c $< -o $@

$(OUT)/q1n1-usb-ucsi-probe.efi: $(OUT)/usb-ucsi-probe.obj
	$(LD) /subsystem:efi_application /entry:efi_main /nodefaultlib /machine:arm64 \
	    /dynamicbase /out:$@ $^

$(OUT)/usb-typec-probe.obj: platform/uefi/usb-typec-probe.c platform/uefi/efi.h
	@mkdir -p $(OUT)
	$(CC) $(CFLAGS) -c $< -o $@

$(OUT)/q1n1-usb-typec-probe.efi: $(OUT)/usb-typec-probe.obj
	$(LD) /subsystem:efi_application /entry:efi_main /nodefaultlib /machine:arm64 \
	    /dynamicbase /out:$@ $^

$(OUT)/usb-ebs-prep.obj: platform/uefi/usb-ebs-prep.c platform/uefi/usb-ebs.h platform/uefi/qcom-dwc3.h platform/uefi/usb-serial.c platform/uefi/usb-role-change.c platform/uefi/usbfn.h platform/uefi/efi.h
	@mkdir -p $(OUT)
	$(CC) $(CFLAGS) -c $< -o $@

$(OUT)/q1n1-usb-ebs-prep.efi: $(OUT)/usb-ebs-prep.obj
	$(LD) /subsystem:efi_application /entry:efi_main /nodefaultlib /machine:arm64 \
	    /dynamicbase /out:$@ $^

# QEMU-only fixture (fake controller in RAM). Never include in a hardware kit.
usb-ebs-fixture: $(OUT)/usb-ebs-fixture.efi
.PHONY: usb-ebs-fixture
$(OUT)/usb-ebs-fixture.obj: platform/uefi/usb-ebs.c platform/uefi/usb-ebs.h platform/uefi/qcom-dwc3.h platform/uefi/fbcon.h platform/uefi/efi.h
	@mkdir -p $(OUT)
	$(CC) $(CFLAGS) -DUSB_EBS_QEMU_FIXTURE -c $< -o $@

$(OUT)/usb-ebs-fixture.efi: $(OUT)/usb-ebs-fixture.obj $(OUT)/qcom-dwc3.obj $(OUT)/fbcon.obj $(OUT)/entry.obj
	$(LD) /subsystem:efi_application /entry:efi_main /nodefaultlib /machine:arm64 \
	    /dynamicbase /out:$@ $^

$(OUT)/usb-ebs.obj: platform/uefi/usb-ebs.c platform/uefi/usb-ebs.h platform/uefi/qcom-dwc3.h platform/uefi/fbcon.h platform/uefi/efi.h
	@mkdir -p $(OUT)
	$(CC) $(CFLAGS) -c $< -o $@

$(OUT)/qcom-dwc3.obj: platform/uefi/qcom-dwc3.c platform/uefi/qcom-dwc3.h
	@mkdir -p $(OUT)
	$(CC) $(CFLAGS) -c $< -o $@

$(OUT)/sysinfo.obj: platform/uefi/sysinfo.c platform/uefi/sysinfo.h platform/uefi/console.h
	@mkdir -p $(OUT)
	$(CC) $(CFLAGS) -c $< -o $@

$(OUT)/console.obj: platform/uefi/console.c platform/uefi/console.h
	@mkdir -p $(OUT)
	$(CC) $(CFLAGS) -c $< -o $@

$(OUT)/fbcon.obj: platform/uefi/fbcon.c platform/uefi/fbcon.h platform/uefi/efi.h
	@mkdir -p $(OUT)
	$(CC) $(CFLAGS) -c $< -o $@

$(OUT)/q1n1-usb-ebs.efi: $(OUT)/usb-ebs.obj $(OUT)/qcom-dwc3.obj $(OUT)/fbcon.obj $(OUT)/entry.obj
	$(LD) /subsystem:efi_application /entry:efi_main /nodefaultlib /machine:arm64 \
	    /dynamicbase /out:$@ $^

$(OUT)/usb-serial-v3.obj: platform/uefi/usb-serial-v3.c platform/uefi/usb-serial.c platform/uefi/usb-role-change.c platform/uefi/usbfn.h platform/uefi/efi.h
	@mkdir -p $(OUT)
	$(CC) $(CFLAGS) -c $< -o $@

$(OUT)/q1n1-usb-serial-v3.efi: $(OUT)/usb-serial-v3.obj
	$(LD) /subsystem:efi_application /entry:efi_main /nodefaultlib /machine:arm64 \
	    /dynamicbase /out:$@ $^

$(OUT)/usb-serial.obj: platform/uefi/usb-serial.c platform/uefi/usb-role-change.c platform/uefi/usbfn.h platform/uefi/efi.h
	@mkdir -p $(OUT)
	$(CC) $(CFLAGS) -c $< -o $@

$(OUT)/q1n1-usb-serial.efi: $(OUT)/usb-serial.obj
	$(LD) /subsystem:efi_application /entry:efi_main /nodefaultlib /machine:arm64 \
	    /dynamicbase /out:$@ $^

$(OUT)/usb-controller-probe.obj: platform/uefi/usb-controller-probe.c platform/uefi/usb-role-change.c platform/uefi/efi.h
	@mkdir -p $(OUT)
	$(CC) $(CFLAGS) -c $< -o $@

$(OUT)/q1n1-usb-controller-probe.efi: $(OUT)/usb-controller-probe.obj
	$(LD) /subsystem:efi_application /entry:efi_main /nodefaultlib /machine:arm64 \
	    /dynamicbase /out:$@ $^

$(OUT)/usb-role-change.obj: platform/uefi/usb-role-change.c platform/uefi/efi.h
	@mkdir -p $(OUT)
	$(CC) $(CFLAGS) -c $< -o $@

$(OUT)/q1n1-usb-role-change.efi: $(OUT)/usb-role-change.obj
	$(LD) /subsystem:efi_application /entry:efi_main /nodefaultlib /machine:arm64 \
	    /dynamicbase /out:$@ $^

$(OUT)/usb-role-probe.obj: platform/uefi/usb-probe.c platform/uefi/efi.h
	@mkdir -p $(OUT)
	$(CC) $(CFLAGS) -DA16_USB_ROLE_PROBE=1 -c $< -o $@

$(OUT)/q1n1-usb-role-probe.efi: $(OUT)/usb-role-probe.obj
	$(LD) /subsystem:efi_application /entry:efi_main /nodefaultlib /machine:arm64 \
	    /dynamicbase /out:$@ $^

# Synthetic protocol data for QEMU tests only. Never include in a hardware kit.
$(OUT)/usb-role-fixture.obj: tools/uefi-usb-role-fixture.c platform/uefi/efi.h
	@mkdir -p $(OUT)
	$(CC) $(CFLAGS) -c $< -o $@

$(OUT)/usb-role-fixture.efi: $(OUT)/usb-role-fixture.obj
	$(LD) /subsystem:efi_boot_service_driver /entry:efi_main /nodefaultlib /machine:arm64 \
	    /dynamicbase /out:$@ $^

$(OUT)/usb-probe.obj: platform/uefi/usb-probe.c platform/uefi/efi.h
	@mkdir -p $(OUT)
	$(CC) $(CFLAGS) -c $< -o $@

$(OUT)/q1n1-usb-probe.efi: $(OUT)/usb-probe.obj
	$(LD) /subsystem:efi_application /entry:efi_main /nodefaultlib /machine:arm64 \
	    /dynamicbase /out:$@ $^

$(OUT)/main.obj: platform/uefi/main.c platform/uefi/efi.h platform/uefi/fbcon.h platform/uefi/boot-window.h \
    platform/uefi/usb-ebs.h platform/uefi/qcom-dwc3.h platform/uefi/q1n1-proxy.h platform/uefi/gicv3.h
	@mkdir -p $(OUT)
	$(CC) $(CFLAGS) -c $< -o $@

$(OUT)/entry.obj: platform/uefi/entry.S data/bootlogo_256.bin data/asahi_bootlogo_256.bin font/font_retina.bin
	@mkdir -p $(OUT)
	$(CC) $(CFLAGS) -c $< -o $@

# q1n1.efi uses recoverable exception vectors; the older test apps keep entry.obj.
$(OUT)/entry-frame.obj: platform/uefi/entry.S data/bootlogo_256.bin data/asahi_bootlogo_256.bin font/font_retina.bin
	@mkdir -p $(OUT)
	$(CC) $(CFLAGS) -DQ1N1_EXCEPTION_FRAME -c $< -o $@

# The boot window reuses the serial-v3 firmware USB code; its echo helpers stay unused.
$(OUT)/boot-window.obj: platform/uefi/boot-window.c platform/uefi/boot-window.h platform/uefi/usb-ebs.h \
    platform/uefi/qcom-dwc3.h platform/uefi/usb-serial.c platform/uefi/usb-role-change.c platform/uefi/usbfn.h \
    platform/uefi/ucsi-console.h platform/uefi/efi.h
	@mkdir -p $(OUT)
	$(CC) $(CFLAGS) -Wno-unused-function -c $< -o $@

# The UCSI console reuses the tested PmicGlink transport; its one-shot helpers stay unused.
$(OUT)/ucsi-console.obj: platform/uefi/ucsi-console.c platform/uefi/ucsi-console.h platform/uefi/usb-ucsi-status.c \
    platform/uefi/usb-ucsi-reg-probe.c platform/uefi/efi.h
	@mkdir -p $(OUT)
	$(CC) $(CFLAGS) -Wno-unused-function -c $< -o $@

$(OUT)/gicv3.obj: platform/uefi/gicv3.c platform/uefi/gicv3.h platform/uefi/efi.h
	@mkdir -p $(OUT)
	$(CC) $(CFLAGS) -c $< -o $@

$(OUT)/xhci.obj: platform/uefi/xhci.c platform/uefi/xhci.h
	@mkdir -p $(OUT)
	$(CC) $(CFLAGS) -c $< -o $@

$(OUT)/ncm.obj: platform/uefi/ncm.c platform/uefi/ncm.h platform/uefi/xhci.h
	@mkdir -p $(OUT)
	$(CC) $(CFLAGS) -c $< -o $@

$(OUT)/ncm-proxy.obj: platform/uefi/ncm-proxy.c platform/uefi/ncm-proxy.h platform/uefi/ncm.h
	@mkdir -p $(OUT)
	$(CC) $(CFLAGS) -c $< -o $@

$(OUT)/q1n1-proxy.obj: platform/uefi/q1n1-proxy.c platform/uefi/q1n1-proxy.h
	@mkdir -p $(OUT)
	$(CC) $(CFLAGS) -c $< -o $@

$(OUT)/proxy-asm.obj: platform/uefi/proxy-asm.S
	@mkdir -p $(OUT)
	$(CC) $(CFLAGS) -c $< -o $@

$(OUT)/q1n1.efi: $(OUT)/main.obj $(OUT)/entry-frame.obj $(OUT)/boot-window.obj $(OUT)/ucsi-console.obj \
    $(OUT)/q1n1-proxy.obj $(OUT)/proxy-asm.obj $(OUT)/qcom-dwc3.obj $(OUT)/gicv3.obj \
    $(OUT)/console.obj $(OUT)/sysinfo.obj $(OUT)/fbcon.obj $(OUT)/xhci.obj $(OUT)/ncm.obj $(OUT)/ncm-proxy.obj
	$(LD) /subsystem:efi_application /entry:efi_main /nodefaultlib /machine:arm64 \
	    /dynamicbase /out:$@ $^

$(OUT)/stage/%.obj: platform/uefi/%.c
	@mkdir -p $(OUT)/stage
	$(CC) $(STAGE_CFLAGS) -c $< -o $@

$(OUT)/stage/%.obj: platform/uefi/%.S data/bootlogo_256.bin data/asahi_bootlogo_256.bin font/font_retina.bin
	@mkdir -p $(OUT)/stage
	$(CC) $(STAGE_CFLAGS) -DQ1N1_EXCEPTION_FRAME -c $< -o $@

# Two link addresses: the A16 reserves 0xb0000000, QEMU's virt machine has RAM
# at 0x40000000, so the payload reserves whichever it can and reports it.
$(OUT)/q1n1-stage.bin: $(STAGE_OBJS) platform/uefi/stage.ld
	$(LLD) -T platform/uefi/stage.ld --defsym=STAGE_BASE=0xb0000000 -o $(OUT)/stage/hardware.elf $(STAGE_OBJS)
	$(OBJCOPY) -O binary $(OUT)/stage/hardware.elf $@

$(OUT)/q1n1-stage-qemu.bin: $(STAGE_OBJS) platform/uefi/stage.ld
	$(LLD) -T platform/uefi/stage.ld --defsym=STAGE_BASE=0x4c000000 -o $(OUT)/stage/qemu.elf $(STAGE_OBJS)
	$(OBJCOPY) -O binary $(OUT)/stage/qemu.elf $@

# A second slot in the same reserved region: a running stage cannot be replaced
# in place, so successive chainloads alternate between the two.
$(OUT)/q1n1-stage-alt.bin: $(STAGE_OBJS) platform/uefi/stage.ld
	$(LLD) -T platform/uefi/stage.ld --defsym=STAGE_BASE=0xb0800000 -o $(OUT)/stage/hardware-alt.elf $(STAGE_OBJS)
	$(OBJCOPY) -O binary $(OUT)/stage/hardware-alt.elf $@

$(OUT)/q1n1-stage-qemu-alt.bin: $(STAGE_OBJS) platform/uefi/stage.ld
	$(LLD) -T platform/uefi/stage.ld --defsym=STAGE_BASE=0x4c800000 -o $(OUT)/stage/qemu-alt.elf $(STAGE_OBJS)
	$(OBJCOPY) -O binary $(OUT)/stage/qemu-alt.elf $@

# Every object's header list, as the compiler saw it. Missing on a first build,
# which is what the leading dash is for.
-include $(wildcard $(OUT)/*.d) $(wildcard $(OUT)/stage/*.d)

clean:
	rm -f $(OUT)/*.d $(OUT)/stage/*.d $(OUT)/stage/*.obj $(OUT)/stage/*.elf \
	    $(OUT)/q1n1-stage.bin $(OUT)/q1n1-stage-alt.bin \
	    $(OUT)/q1n1-stage-qemu.bin $(OUT)/q1n1-stage-qemu-alt.bin
	rm -f $(OUT)/*.obj $(OUT)/q1n1.efi $(OUT)/q1n1-usb-probe.efi \
	    $(OUT)/q1n1-usb-role-probe.efi $(OUT)/usb-role-fixture.efi \
	    $(OUT)/q1n1-usb-role-change.efi $(OUT)/q1n1-usb-serial.efi $(OUT)/q1n1-usb-typec-probe.efi \
	    $(OUT)/q1n1-usb-ucsi-probe.efi $(OUT)/q1n1-usb-usbc-probe.efi $(OUT)/q1n1-usb-ucsi-reg-probe.efi \
	    $(OUT)/q1n1-usb-ucsi-status.efi $(OUT)/q1n1-usb-ucsi-status-v2.efi $(OUT)/q1n1-usb-ucsi-device0.efi \
	    $(OUT)/q1n1-usb-controller-probe.efi $(OUT)/q1n1-usb-serial-v3.efi \
	    $(OUT)/q1n1-usb-ebs-prep.efi $(OUT)/q1n1-usb-ebs.efi $(OUT)/usb-ebs-fixture.efi

/* SPDX-License-Identifier: MIT */
/* Live battery readings in the EL2 proxy, after ExitBootServices.
 *
 * The boot window reads the battery through the firmware's PmicGlinkDxe
 * (battery.h). That driver is gone after ExitBootServices, so this path
 * brings up q1n1's own GLINK channel to the SoCCP's PMIC router over SMEM
 * (smem.h, glink-smem.h) and asks the same registers (pmic-router.h).
 *
 * Entry is one function the host calls with P_CALL (bootinfo battery_call):
 *     x0 = flags, x1 = timeout in ms (0: 1500), x2 = SMEM host (0: 19),
 *     x3 = SMEM base override (0: target info, else glymur DT), x4 = size
 * It returns a status (0 or a negative code below) and fills the published
 * record (bootinfo battery_result). tools/a16ctl.py battery drives it.
 *
 * Everything the stage touches for the first time -- the target-info word,
 * the SMEM header and table pages, each page of the SoCCP partition, the IPCC
 * frame -- is read under the proxy's exception guard first, so a wrong
 * address is a refusal, not a crash. That covers reads only: a posted write
 * to Device memory (the doorbell) can fault later as an asynchronous SError,
 * which the guard does not catch; the doorbell address itself is proven
 * (IPCCDxe, below) and the page must be mapped Device.
 *
 * Nobody-reading rule. While a session is STARTED the SoCCP treats APPS as up
 * and may send on the channel at any time (battery-manager and UCSI
 * notifications, owners 0x800a/0x800b, and requests for more receive
 * intents). The status tick therefore services the session every 20 ms
 * (q1n1_glink_service: bounded, never waits, never restarts), so those are
 * consumed and answered between calls; a chainloaded stage resumes the
 * session from its first tick. Before leaving q1n1 for anything that is not
 * a q1n1 stage, the session is released (Q1N1_LIVE_RELEASE: the firmware's
 * own glink_ssr DO_CLEANUP), which main.c does on a P_VECTOR whose argument
 * is not q1n1's bootinfo. */
#pragma once
#include <stdint.h>
#include "glink-smem.h"
#include "pmic-router.h"

/* IPCC: the APSS frame of the MPROC protocol and SoCCP's GLINK doorbell.
 * Firmware: IPCCDxe .data 0x23b0 {protocol 0, APSS client 8, base 0x03E00000},
 * frame = base + index(client 8 in the table at .data 0x2010 = 4) << 12
 * (RVA 0x16e8-0x16f8) = 0x03E04000; SEND is frame + 0x0C written as
 * client << 16 | signal (RVA 0x1c3c-0x1c50); the version register is frame
 * + 0 (RVA 0x1700). Public: glymur.dtsi ipcc@3e04000, IPCC_MPROC_SOCCP 46,
 * IPCC_MPROC_SIGNAL_GLINK_QMP 0; qcom-ipcc.c SEND_ID 0x0C. */
#define Q1N1_IPCC_FRAME 0x03e04000ull
#define Q1N1_IPCC_SEND 0x0cu
#define Q1N1_IPCC_SOCCP_GLINK 0x002e0000u
#define Q1N1_SOCCP_HOST 19u                  /* glymur.dtsi qcom,remote-pid for the SoCCP edge */
#define Q1N1_SOCCP_SMP2P_IN 617u             /* "slave-kernel": bit 0 fatal, bit 1 ready */
#define Q1N1_PMIC_CHANNEL "PMIC_RTR_SOCCP_APPS"
#define Q1N1_PMIC_LCID 1u

/* P_CALL flags.
 *
 * Restart policy. A call continues the session when it can (STARTED, our
 * index words untouched). Otherwise the link has to be restarted, and q1n1
 * does that by itself only when all four descriptor words read 0 -- the
 * cleaned-up state the firmware's ExitBootServices callback leaves (see
 * glink-smem.h), in which Linux's restart (LINUX mode) is what the SoCCP
 * expects -- and only in LINUX mode. Any other state needs an explicit mode
 * flag (exactly one); without it the call refuses (ENEEDMODE) before writing.
 * RESTART without a mode follows the same rule. LINUX and DRAIN set our head
 * to 0, so with word 0 (the SoCCP's tail in our FIFO) or word 3 (its head in
 * its own) not 0 -- a SoCCP that never cleaned up -- it would read our FIFO
 * from its old tail, and LINUX would parse its old frames: both refuse there
 * (EWORDS) unless FORCE is given too. The refresh and the resume of a
 * chainloaded session never restart (ENORESTART).
 *
 * One session at a time. While a session is STARTED, a call that names
 * another SMEM base, size or host refuses (EOTHER) before binding, since
 * binding there would drop the session the SoCCP still sees; release it
 * first. A survey never binds: it reads through a separate view
 * (q1n1_glink_view) and leaves the session alone. */
#define Q1N1_LIVE_SURVEY (1u << 0)         /* read-only: locate and report, write nothing */
#define Q1N1_LIVE_RESTART (1u << 1)        /* restart even when the session could continue */
#define Q1N1_LIVE_LINUX (1u << 2)          /* restart mode LINUX */
#define Q1N1_LIVE_DRAIN (1u << 3)          /* restart mode DRAIN */
#define Q1N1_LIVE_RESYNC (1u << 4)         /* restart mode RESYNC */
#define Q1N1_LIVE_UCSI_VERSION (1u << 5)   /* read UCSI VERSION (known answer 0x0210) instead */
#define Q1N1_LIVE_ATTR_UNKNOWN_OK (1u << 6) /* proceed when a page's attribute cannot be read; never
                                            * when it is known to be cacheable, or the doorbell not Device */
#define Q1N1_LIVE_REFRESH_ON (1u << 7)     /* status panel re-reads every 30 s (never restarts) */
#define Q1N1_LIVE_REFRESH_OFF (1u << 8)
#define Q1N1_LIVE_RELEASE (1u << 9)        /* send the firmware's DO_CLEANUP and leave all four words 0 */
#define Q1N1_LIVE_NO_RESTART (1u << 10)    /* continue a session or refuse (refresh, resume) */
#define Q1N1_LIVE_RESUME (1u << 11)        /* bind and service a continued session; no register reads */
#define Q1N1_LIVE_FORCE (1u << 12)         /* with LINUX or DRAIN: restart even when word 0 or 3 is not 0 */
#define Q1N1_LIVE_MODES (Q1N1_LIVE_LINUX | Q1N1_LIVE_DRAIN | Q1N1_LIVE_RESYNC)

enum {
    Q1N1_LIVE_OK = 0,
    Q1N1_LIVE_EWORDS = -6,       /* LINUX/DRAIN with the SoCCP's words (0, 3) not 0: add FORCE to mean it */
    Q1N1_LIVE_EOTHER = -7,       /* a STARTED session is on another SMEM/host: release it first */
    Q1N1_LIVE_EATTR = -8,        /* a page's mapping attribute could not be read */
    Q1N1_LIVE_EFLAGS = -9,       /* conflicting flags (two modes, or release with a restart or survey) */
    Q1N1_LIVE_EFAULT = -10,      /* a guarded access faulted: address not mapped at EL2 */
    Q1N1_LIVE_ECACHED = -11,     /* an SMEM page we touch is mapped cacheable */
    Q1N1_LIVE_EREMOTE = -12,     /* SoCCP SMP2P missing, malformed, fatal or not ready */
    Q1N1_LIVE_EBUSY = -13,       /* re-entered */
    Q1N1_LIVE_EIPCC = -14,       /* IPCC frame version is not 2.x */
    Q1N1_LIVE_ENEEDMODE = -15,   /* a restart is needed and the words are not all 0: give a mode */
    Q1N1_LIVE_ENORESTART = -16,  /* refresh/resume: the session cannot continue, and they never restart */
    Q1N1_LIVE_EDEVICE = -17,     /* the IPCC doorbell page is not mapped Device */
    Q1N1_LIVE_ENOSESSION = -18,  /* release: no healthy q1n1 session and the words are not all 0 */
    Q1N1_LIVE_ECLOCK = -19,      /* the counter reads 0 (no timer frequency): no deadline would hold */
};

enum q1n1_live_phase {
    Q1N1_LIVE_PHASE_TARGET = 1, Q1N1_LIVE_PHASE_SMEM, Q1N1_LIVE_PHASE_PARTITION, Q1N1_LIVE_PHASE_ITEMS,
    Q1N1_LIVE_PHASE_SMP2P, Q1N1_LIVE_PHASE_IPCC, Q1N1_LIVE_PHASE_MAPPING, Q1N1_LIVE_PHASE_BIND,
    Q1N1_LIVE_PHASE_SESSION, Q1N1_LIVE_PHASE_OPEN, Q1N1_LIVE_PHASE_READ, Q1N1_LIVE_PHASE_DONE,
    Q1N1_LIVE_PHASE_RELEASE,
};

#define Q1N1_LIVE_MAGIC UINT64_C(0x54544142314e3151) /* "Q1N1BATT" */
#define Q1N1_LIVE_PARTITIONS 24u
#define Q1N1_LIVE_TEXT 160u

/* Published through bootinfo; all u64 except the trailing text, append only.
 * tools/a16ctl.py parses it by name in this order. */
struct q1n1_live_battery {
    uint64_t magic, version, size;
    uint64_t calls, successes, status, phase, flags, elapsed_us;
    uint64_t plausible, state, rate, remaining, voltage, unit, design, full, cycles, ac, percent;
    uint64_t ucsi_version;
    uint64_t target_pointer, target_status, target_base, target_size;
    uint64_t smem_base, smem_size, smem_status, smem_version, smem_items, ptable_entries;
    uint64_t remote_host, partition_status, partition_offset, partition_size, partition_cacheline;
    uint64_t desc_offset, tx_offset, tx_length, tx_cached, rx_offset, rx_length, rx_cached;
    uint64_t desc0, desc1, desc2, desc3, sample_status;
    uint64_t smp2p_offset, smp2p_status, smp2p_value;
    uint64_t smem_attr, ipcc_attr, ipcc_version, doorbell_address, doorbell_value;
    uint64_t glink_state, glink_error, glink_command, glink_mode, lcid, rcid, features;
    uint64_t version_acked, remote_version, open_sent, open_acked, remote_opened, remote_intents;
    uint64_t restarts, adoptions, tx_frames, rx_frames, doorbells, messages, dropped, foreign_opens,
             protocol_errors;
    uint64_t pmic_requests, pmic_replies, pmic_stale, pmic_unsolicited, pmic_foreign, pmic_bad;
    uint64_t reply_owner, reply_type, reply_opcode, reply_length, reply_error;
    uint64_t desc_attr, tx_attr, rx_attr;          /* worst attribute over the item's pages */
    uint64_t release_status, releases, cleanup_sent, cleanup_done, ssr_opened;
    uint64_t service_polls, service_frames, version_zero_acks, remote_closes, adopt_refused;
    uint64_t desc_end0, desc_end1, desc_end2, desc_end3;   /* descriptor as this call left it */
    uint64_t partitions;                           /* table rows below; headers are not read */
    uint64_t partition[Q1N1_LIVE_PARTITIONS][4];  /* host0 | host1 << 16, offset, size, cacheline */
    char text[Q1N1_LIVE_TEXT];
};
/* tools/q1n1proxy.py LIVE_BATTERY_FIELDS lists the 107 leading u64 by name. */
_Static_assert(__builtin_offsetof(struct q1n1_live_battery, partition) == 107 * 8, "live battery layout");
_Static_assert(sizeof(struct q1n1_live_battery) == (107 + 4 * Q1N1_LIVE_PARTITIONS) * 8 + Q1N1_LIVE_TEXT,
               "live battery size");

/* How the stage reaches memory. Hardware: main.c; tests: a simulated SoC. */
struct q1n1_live_platform {
    /* Guarded copy with naturally aligned 32-bit loads; 0 or Q1N1_LIVE_EFAULT. */
    int (*read)(void *ctx, void *dst, uint64_t phys, uint32_t size);
    int (*write32)(void *ctx, uint64_t phys, uint32_t value);           /* guarded */
    volatile uint8_t *(*map)(void *ctx, uint64_t phys, uint64_t size); /* the CPU address */
    int (*attr)(void *ctx, uint64_t phys, uint32_t *mair_attr);        /* 0, or <0 unknown */
    int (*in_memory_map)(void *ctx, uint64_t phys, uint64_t size);     /* inside an EFI descriptor */
    uint64_t (*now_us)(void *ctx);
    void (*relax)(void *ctx);
    void *ctx;
};

int q1n1_live_battery_run(const struct q1n1_live_platform *pf, uint64_t flags, uint64_t timeout_ms,
                          uint64_t host, uint64_t smem_base, uint64_t smem_size);
/* The status tick: services a STARTED session every 20 ms (resuming a
 * chainloaded one first), and runs the refresh when enabled. Never restarts. */
void q1n1_live_battery_tick(const struct q1n1_live_platform *pf);
/* Before leaving q1n1 for code that is not a q1n1 stage: release a STARTED
 * session (Q1N1_LIVE_RELEASE with the session's own SMEM and host). 0 when
 * there was nothing to release. */
int q1n1_live_battery_release(const struct q1n1_live_platform *pf);

/* The published record and the session a chainloaded stage continues. */
struct q1n1_live_battery *q1n1_live_battery_result(void);
struct q1n1_glink_session *q1n1_live_battery_session(void);
/* Copy a predecessor's session (bootinfo battery_session of the old stage).
 * Copied only when magic, version and size match exactly; anything else is
 * ignored (and noted in adopt_refused), so the next call finds our words
 * changed or unknown and applies the restart policy above. */
void q1n1_live_battery_adopt(const void *session, uint64_t size);

/* Walk a 4 KiB-granule stage-1 table (TTBR0/TCR/MAIR of the current EL) and
 * return the MAIR attribute byte for va. read64 is a guarded load. Returns 0,
 * or <0 when the walk cannot be completed (unsupported format, fault, not
 * mapped). */
int q1n1_mmu_attr(uint64_t ttbr, uint64_t tcr, uint64_t mair, uint64_t va,
                  int (*read64)(void *ctx, uint64_t phys, uint64_t *value), void *ctx, uint32_t *attr);
/* 1 for Device-nGnRnE..GRE or Normal Non-cacheable (inner and outer). */
int q1n1_mmu_attr_uncached(uint32_t attr);
/* 1 for Device-nGnRnE..GRE only. */
int q1n1_mmu_attr_device(uint32_t attr);

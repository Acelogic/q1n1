/* SPDX-License-Identifier: MIT */
/* Read an existing register buffer through the exact BIOS312 transport.
 * No UCSI command, role request, reset, connect, or notification change is sent.
 * Default: GLINK owner 0x800b/opcode 0x11 to fetch 48 UCSI bytes.
 * Q1N1_USBC_BUFFER_PROBE: owner 0x800c/opcode 0x14 to fetch 132 USB-C bytes.
 */
#include "efi.h"
#define GLINK_SIZE 0x6000
#define GLINK_CODE_FNV UINT64_C(0x391a635ac9656c89)
#define GLINK_PROTOCOL_RVA 0x40b0
#ifdef Q1N1_USBC_BUFFER_PROBE
#define GLINK_READ_RVA 0x1b1c
#define GLINK_READ_SIZE 132
#define PROBE_NAME "USB-C"
#else
#define GLINK_READ_RVA 0x19e4
#define GLINK_READ_SIZE 48
#define PROBE_NAME "UCSI"
#endif
static efi_guid loaded_guid={0x5b1b31a1,0x9562,0x11d2,{0x8e,0x3f,0,0xa0,0xc9,0x69,0x72,0x3b}};
static efi_guid glink_guid={0x7429eb07,0xf2e3,0x4a6b,{0xaa,0x96,0xc9,0x09,0xcd,0x95,0x6e,0xf7}};
static const uint16_t callback_rvas[]={
    0x19e4,0x1a70,0x1b1c,0x1ba8,0x1c4c,0x1c58,0x1d18,0x1d70,
    0x1f34,0x1f5c,0x2118,0x2140,0x21b4,0x220c,0x2210,0x2270,
    0x2334,0x23f8,0x245c,0x2460,0x24b8,0x24bc,0x2548,0x25f8
};
static void out(struct efi_system_table *st,const char *s)
{
    char16 text[120];
    while (*s) {
        unsigned n=0;
        while (*s && n<117) { if (*s=='\n') text[n++]='\r';text[n++]=(uint8_t)*s++; }
        text[n]=0;st->console_out->output(st->console_out,text);
    }
}
static void number(struct efi_system_table *st,const char *label,uint64_t value)
{
    char text[19]="0x0000000000000000";
    for (unsigned n=0;n<16;n++) text[n+2]="0123456789ABCDEF"[(value>>(60-4*n))&15];
    out(st,label);out(st,text);out(st,"\n");
}
static efi_status refuse(struct efi_system_table *st,const char *reason)
{
    out(st,"REFUSED: ");out(st,reason);out(st,"\nNo " PROBE_NAME " read requested. Returning to shell.\n");
    return EFI_UNSUPPORTED;
}
static uint64_t hash_code(const uint8_t *p)
{
    uint64_t h=UINT64_C(0xcbf29ce484222325);
    for (unsigned n=0x1000;n<0x4000;n++) h=(h^p[n])*UINT64_C(0x100000001b3);
    return h;
}
static uint8_t *find_driver(struct efi_system_table *st)
{
    efi_handle *handles=NULL;uint64_t count=0;unsigned matches=0;uint8_t *found=NULL;
    if (st->boot->locate_handle_buffer(EFI_BY_PROTOCOL,&loaded_guid,NULL,&count,&handles)) return NULL;
    for (uint64_t n=0;n<count;n++) {
        struct efi_loaded_image *li=NULL;
        if (st->boot->handle_protocol(handles[n],&loaded_guid,(void **)&li) || !li ||
            !li->image_base || li->image_size!=GLINK_SIZE) continue;
        if (hash_code(li->image_base)==GLINK_CODE_FNV) { found=li->image_base;matches++; }
    }
    st->boot->free_pool(handles);return matches==1 ? found : NULL;
}
static uint64_t little(const uint8_t *p,unsigned bytes)
{
    uint64_t value=0;for (unsigned n=0;n<bytes;n++) value|=(uint64_t)p[n]<<(8*n);return value;
}
static efi_status request_buffer(uint8_t *driver,uint8_t **buffer)
{
#ifdef Q1N1_UCSI_HOST_TEST
    extern efi_status mock_ucsi_read(uint8_t **,uint8_t);
    (void)driver;return mock_ucsi_read(buffer,GLINK_READ_SIZE);
#else
    typedef efi_status (*read_fn)(uint8_t **,uint8_t);
    return ((read_fn)(driver+GLINK_READ_RVA))(buffer,GLINK_READ_SIZE);
#endif
}
efi_status efi_main(efi_handle image,struct efi_system_table *st)
{
    (void)image;
    if (!st || !st->boot || !st->console_out) return EFI_UNSUPPORTED;
    out(st,"q1n1 A16 BIOS312 " PROBE_NAME " buffer read v1\n"
           "Fetch existing registers only; no UCSI role or reset command.\n");
    uint8_t *driver=find_driver(st);
    if (!driver) return refuse(st,"Matching BIOS312 PmicGlinkDxe code not found.");
    efi_handle *handles=NULL;uint64_t count=0;unsigned matches=0;
    if (st->boot->locate_handle_buffer(EFI_BY_PROTOCOL,&glink_guid,NULL,&count,&handles))
        return refuse(st,"PMIC GLINK protocol absent.");
    for (uint64_t n=0;n<count;n++) {
        void *p=NULL;
        if (!st->boot->handle_protocol(handles[n],&glink_guid,&p) && p==driver+GLINK_PROTOCOL_RVA) matches++;
    }
    st->boot->free_pool(handles);
    if (!matches) return refuse(st,"Installed GLINK interface differs from inspected object.");
    const uintptr_t *protocol=(const uintptr_t *)(driver+GLINK_PROTOCOL_RVA);
    if (protocol[0]!=0x1000c) return refuse(st,"PMIC GLINK revision mismatch.");
    for (unsigned n=0;n<sizeof(callback_rvas)/sizeof(callback_rvas[0]);n++)
        if (protocol[n+1]!=(uintptr_t)(driver+callback_rvas[n]))
            return refuse(st,"PMIC GLINK callback layout mismatch.");
    unsigned ready=*(volatile uint8_t *)(driver+0x43e1);
    number(st,"GLINK transport ready flag: ",ready);
    if (ready!=1 || *(uintptr_t *)(driver+0x4238)!=(uintptr_t)st->boot)
        return refuse(st,"GLINK transport or boot-services reference is not ready.");
    efi_status status=st->boot->set_watchdog(30,0,0,NULL);
    if (status) return refuse(st,"Could not arm the firmware watchdog.");
    /* Byte guards keep the trailing canary immediately after the 132-byte buffer. */
    struct { uint8_t before[8],data[GLINK_READ_SIZE],after[8]; } result;
    for (unsigned n=0;n<8;n++) result.before[n]=result.after[n]=(uint8_t)(0x51+n);
    for (unsigned n=0;n<GLINK_READ_SIZE;n++) result.data[n]=0xa5;
    uint8_t *buffer=result.data;
#ifdef Q1N1_USBC_BUFFER_PROBE
    out(st,"Requesting one 132-byte Qualcomm USB-C status read...\n");
#else
    out(st,"Requesting one 48-byte UCSI buffer read...\n");
#endif
    status=request_buffer(driver,&buffer);
    efi_status watchdog=st->boot->set_watchdog(0,0,0,NULL);
    number(st,PROBE_NAME " transport read status: ",status);
    if (watchdog) number(st,"Watchdog disable status: ",watchdog);
    unsigned guards=1,changed=0;
    for (unsigned n=0;n<8;n++)
        guards&=result.before[n]==(uint8_t)(0x51+n) && result.after[n]==(uint8_t)(0x51+n);
    for (unsigned n=0;n<GLINK_READ_SIZE;n++) changed+=result.data[n]!=0xa5;
    if (!guards || buffer!=result.data) {
        out(st,"Unexpected output buffer contract. Returning to shell.\n");return EFI_UNSUPPORTED;
    }
    if (status) { out(st,"Read failed; no UCSI command sent. Returning to shell.\n");return status; }
    if (!changed) {
        out(st,"Output buffer was not populated. Returning to shell.\n");
        return EFI_UNSUPPORTED;
    }
#ifdef Q1N1_USBC_BUFFER_PROBE
    number(st,"Bytes differing from caller sentinel: ",changed);
    for (unsigned offset=0;offset<GLINK_READ_SIZE;offset+=8) {
        char label[]="RAW +000: ";
        label[5]="0123456789ABCDEF"[(offset>>8)&15];
        label[6]="0123456789ABCDEF"[(offset>>4)&15];
        label[7]="0123456789ABCDEF"[offset&15];
        unsigned remaining=GLINK_READ_SIZE-offset;
        number(st,label,little(result.data+offset,remaining<8 ? remaining : 8));
    }
    out(st,"Raw vendor buffer; connector mapping and data role unconfirmed.\n"
           "EFI success does not validate the remote response error code.\n"
           "USB-C snapshot complete. Returning to shell.\n");
#else
    number(st,"UCSI VERSION (raw): ",little(result.data,2));
    number(st,"UCSI CCI (raw): ",little(result.data+4,4));
    number(st,"UCSI CONTROL (raw): ",little(result.data+8,8));
    number(st,"MESSAGE IN 0: ",little(result.data+16,8));
    number(st,"MESSAGE IN 1: ",little(result.data+24,8));
    number(st,"MESSAGE OUT 0: ",little(result.data+32,8));
    number(st,"MESSAGE OUT 1: ",little(result.data+40,8));
    out(st,"Existing buffer only; connector status was not requested.\n"
           "UCSI snapshot complete. Returning to shell.\n");
#endif
    return watchdog;
}

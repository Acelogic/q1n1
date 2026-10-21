/* SPDX-License-Identifier: MIT */
/* BIOS312 GIO register reads matching the running A16 Windows PMIC driver.
 * Only VERSION (0x20100) and CCI (0x20104), four bytes each. No writes.
 * The transport's generic completion flag cannot prove response freshness.
 */
#include "efi.h"
#define GLINK_SIZE 0x6000
#define GLINK_CODE_FNV UINT64_C(0x391a635ac9656c89)
#define GLINK_PROTOCOL_RVA 0x40b0
#define GLINK_READ_RVA 0x263c
#define PROBE_NAME "UCSI register"
static efi_guid loaded_guid={0x5b1b31a1,0x9562,0x11d2,{0x8e,0x3f,0,0xa0,0xc9,0x69,0x72,0x3b}};
static efi_guid glink_guid={0x7429eb07,0xf2e3,0x4a6b,{0xaa,0x96,0xc9,0x09,0xcd,0x95,0x6e,0xf7}};
static const uint16_t callback_rvas[]={
    0x19e4,0x1a70,0x1b1c,0x1ba8,0x1c4c,0x1c58,0x1d18,0x1d70,
    0x1f34,0x1f5c,0x2118,0x2140,0x21b4,0x220c,0x2210,0x2270,
    0x2334,0x23f8,0x245c,0x2460,0x24b8,0x24bc,0x2548,0x25f8,
    0x263c,0x2710,0x2804,0x28e8,0x2910
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

static efi_status request_register(uint8_t *driver,uint32_t reg,uint8_t *buffer)
{
    if (reg!=0x20100 && reg!=0x20104) return EFI_UNSUPPORTED;
#ifdef Q1N1_UCSI_REG_HOST_TEST
    extern efi_status mock_register_read(uint32_t,uint8_t *,uint32_t);
    (void)driver;return mock_register_read(reg,buffer,4);
#else
    typedef efi_status (*read_fn)(uint32_t,void *,uint32_t);
    return ((read_fn)(driver+GLINK_READ_RVA))(reg,buffer,4);
#endif
}
static efi_status read_value(struct efi_system_table *st,uint8_t *driver,uint32_t reg,
                             const char *label,uint32_t *value)
{
    struct { uint8_t before[8],data[4],after[8]; } result;
    for (unsigned i=0;i<8;i++) result.before[i]=result.after[i]=(uint8_t)(0x51+i);
    for (unsigned i=0;i<4;i++) result.data[i]=0xa5;
    number(st,"Reading register: ",reg);
    efi_status status=request_register(driver,reg,result.data);
    number(st,"Register read EFI status: ",status);
    number(st,"Firmware cached remote status: ",little(driver+0x4950,4));
    unsigned intact=1,changed=0;
    for (unsigned i=0;i<8;i++)
        intact&=result.before[i]==(uint8_t)(0x51+i) && result.after[i]==(uint8_t)(0x51+i);
    for (unsigned i=0;i<4;i++) changed+=result.data[i]!=0xa5;
    if (!intact) { out(st,"Output buffer guard failed.\n");return EFI_UNSUPPORTED; }
    if (status) return status;
    if (little(driver+0x4950,4)) { out(st,"Remote status was not success.\n");return EFI_UNSUPPORTED; }
    if (!changed) { out(st,"Output buffer was not populated.\n");return EFI_UNSUPPORTED; }
    *value=(uint32_t)little(result.data,4);number(st,label,*value);return 0;
}
efi_status efi_main(efi_handle image,struct efi_system_table *st)
{
    (void)image;
    if (!st || !st->boot || !st->console_out) return EFI_UNSUPPORTED;
    out(st,"q1n1 A16 BIOS312 UCSI register read v1\n"
           "Read VERSION and CCI through GLINK 0x8011/0x90. No UCSI commands.\n");
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
            return refuse(st,"Extended PMIC GLINK callback layout mismatch.");
    unsigned ready=*(volatile uint8_t *)(driver+0x43e1);
    number(st,"GLINK transport ready flag: ",ready);
    if (ready!=1 || *(uintptr_t *)(driver+0x4238)!=(uintptr_t)st->boot)
        return refuse(st,"GLINK transport or boot-services reference is not ready.");
    efi_status status=st->boot->set_watchdog(30,0,0,NULL);
    if (status) return refuse(st,"Could not arm the firmware watchdog.");
    uint32_t version=0,cci=0;
    status=read_value(st,driver,0x20100,"UCSI VERSION register: ",&version);
    if (!status && version!=0x210) {
        out(st,"VERSION differs from the Windows reference 0x0210; stopping reads.\n");
        status=EFI_UNSUPPORTED;
    }
    if (!status && *(volatile uint8_t *)(driver+0x43e1)!=1) {
        out(st,"GLINK became unavailable; stopping reads.\n");status=EFI_UNSUPPORTED;
    }
    if (!status) status=read_value(st,driver,0x20104,"UCSI CCI register: ",&cci);
    efi_status watchdog=st->boot->set_watchdog(0,0,0,NULL);
    if (watchdog) number(st,"Watchdog disable status: ",watchdog);
    out(st,"No reset, notification, connector-status, or role command was sent.\n"
           "Firmware cache/generic completion cannot prove a fresh response.\n");
    if (status) out(st,"Register probe incomplete. Returning to shell.\n");
    else out(st,"Register probe complete. Returning to shell.\n");
    return status ? status : watchdog;
}

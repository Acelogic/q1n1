/* SPDX-License-Identifier: MIT */
/* BIOS312 UCSI 2.1 connector queries. No reset, role, power or mux commands.
 * Reuse the physically tested register preflight before enabling command notifications.
 */
#ifndef Q1N1_UCSI_REG_EMBEDDED
#define efi_main ucsi_register_preflight
#include "usb-ucsi-reg-probe.c"
#undef efi_main
#endif
#define CCI_COMPLETE (1u<<31)
#define CCI_ACK (1u<<29)
#define CCI_BUSY (1u<<28)
#define CCI_FAILURE ((1u<<30)|(1u<<25)|(1u<<26)|(1u<<27))
#ifndef UCSI_STATUS_VERSION
#define UCSI_STATUS_VERSION "v1"
#endif

static int query_allowed(uint64_t command)
{
#ifdef Q1N1_UCSI_CONSOLE
    /* The console supplies connector numbers and role bits from the host, so it
     * gates on the opcode instead. Excluded: CANCEL (can wedge an unresolved
     * command), SET_NEW_CAM (alternate modes drive the display) and
     * SET_POWER_LEVEL (charging). Connector reset and power-role swap are
     * allowed but only ever aimed by the operator. */
    switch ((uint8_t)command) {
    case 0x01: case 0x03: case 0x04: case 0x05: case 0x06: case 0x07:
    case 0x08: case 0x09: case 0x0b: case 0x0c: case 0x0d: case 0x0e:
    case 0x10: case 0x11: case 0x12: case 0x13:
        return 1;
    default:
        return 0;
    }
#else
    /* Exact commands, including all reserved bits; never a generic writer. */
#ifdef UCSI_DATA_ROLE_REQUEST
    if (command==0x3010009 || command==0x10013) return 1;
#endif
    return command==0x10005 || command==5 || command==0x20004 || command==6 ||
           command==0x10007 || command==0x20007 || command==0x10012 || command==0x20012;
#endif
}
static efi_status property_read(uint8_t *driver,uint32_t reg,uint8_t *data,uint32_t size)
{
#ifdef UCSI_STATUS_FULL_BLOCK
    if (!((reg==0x20104 && size==4) || (reg==0x20200 && size==64)))
#else
    if (!((reg==0x20104 && size==4) || (reg==0x20200 && size>0 && size<=32)))
#endif
        return EFI_UNSUPPORTED;
#ifdef Q1N1_UCSI_STATUS_HOST_TEST
    extern efi_status mock_property_read(uint32_t,uint8_t *,uint32_t);
    (void)driver;return mock_property_read(reg,data,size);
#else
    typedef efi_status (*read_fn)(uint32_t,void *,uint32_t);
    return ((read_fn)(driver+0x263c))(reg,data,size);
#endif
}
static efi_status control_write(uint8_t *driver,uint64_t command)
{
    if (!query_allowed(command)) return EFI_UNSUPPORTED;
#ifdef Q1N1_UCSI_STATUS_HOST_TEST
    extern efi_status mock_control_write(uint64_t);
    (void)driver;return mock_control_write(command);
#else
    typedef efi_status (*write_fn)(uint32_t,const void *,uint32_t);
    return ((write_fn)(driver+0x2710))(0x20108,&command,8);
#endif
}
static efi_status guarded_read(struct efi_system_table *st,uint8_t *driver,uint32_t reg,
                               uint8_t *data,unsigned size)
{
    uint8_t buffer[80];
    if (!size || size>64 || *(volatile uint8_t *)(driver+0x43e1)!=1) return EFI_UNSUPPORTED;
    for (unsigned i=0;i<sizeof(buffer);i++) buffer[i]=0xa5;
    efi_status status=property_read(driver,reg,buffer+8,size);
    for (unsigned i=0;i<8;i++) if (buffer[i]!=0xa5 || buffer[8+size+i]!=0xa5) {
        out(st,"Property output guard failed.\n");return EFI_UNSUPPORTED;
    }
    if (status || little(driver+0x4950,4)) {
        number(st,"Property read EFI status: ",status);
        number(st,"Cached remote status: ",little(driver+0x4950,4));return EFI_ERROR(7);
    }
    unsigned changed=0;
    for (unsigned i=0;i<size;i++) {changed+=buffer[i+8]!=0xa5;data[i]=buffer[i+8];}
    if (!changed) {out(st,"Property buffer unchanged.\n");return EFI_UNSUPPORTED;}
    return 0;
}
#ifdef UCSI_STATUS_FULL_BLOCK
static efi_status message_read(struct efi_system_table *st,uint8_t *driver,uint8_t *data,
                               unsigned size,uint32_t cci)
{
    uint8_t previous[64]={0},sample[64];
    for (unsigned n=0;n<4;n++) {
        efi_status status=guarded_read(st,driver,0x20200,sample,64);
        if (status) return status;
        number(st,"MESSAGE_IN sample: ",n+1);
        number(st,"MESSAGE_IN first word: ",little(sample,4));
        unsigned equal=1;
        for (unsigned i=0;i<size;i++) equal&=sample[i]==previous[i];
        /* Require two agreeing samples after at least three same-register reads.
         * A repeated copy of the immediately preceding CCI is not a capability reply.
         * This detects the observed stale-prefix failure; it is not a transport transaction ID.
         */
        if (n>=2 && equal && little(sample,4)!=cci) {
            for (unsigned i=0;i<size;i++) data[i]=sample[i];
            return 0;
        }
        for (unsigned i=0;i<size;i++) previous[i]=sample[i];
        status=((efi_status (*)(uint64_t))st->boot->stall)(20000);
        if (status) return status;
    }
    out(st,"MESSAGE_IN did not stabilize or retained the preceding CCI; stopping.\n");
    return EFI_UNSUPPORTED;
}
#endif
static efi_status get_cci(struct efi_system_table *st,uint8_t *driver,uint32_t *cci)
{
    uint8_t data[4];efi_status status=guarded_read(st,driver,0x20104,data,4);
    if (!status) *cci=(uint32_t)little(data,4);
    return status;
}
static efi_status send_control(struct efi_system_table *st,uint8_t *driver,uint64_t command)
{
    if (*(volatile uint8_t *)(driver+0x43e1)!=1) return EFI_UNSUPPORTED;
    number(st,"UCSI query/control: ",command);
    efi_status status=control_write(driver,command);
    number(st,"Control write EFI status: ",status);
    if (status) return status;
    if (little(driver+0x4950,4)) return EFI_ERROR(7);
    return 0;
}
static efi_status wait_cci(struct efi_system_table *st,uint8_t *driver,uint32_t wanted,uint32_t *cci)
{
    for (unsigned i=0;i<50;i++) {
        efi_status status=get_cci(st,driver,cci);
        if (status) return status;
        if (*cci & CCI_FAILURE) {number(st,"CCI failure: ",*cci);return EFI_ERROR(7);}
        if (!(*cci & CCI_BUSY) && (*cci & wanted) && !(*cci & (wanted==CCI_ACK ? CCI_COMPLETE : CCI_ACK))) {
            number(st,"Completed CCI: ",*cci);return 0;
        }
        status=((efi_status (*)(uint64_t))st->boot->stall)(20000);
        if (status) return status;
    }
    number(st,"CCI timeout: ",*cci);return EFI_ERROR(18);
}
static efi_status run_query(struct efi_system_table *st,uint8_t *driver,uint64_t command,
                            uint8_t *data,unsigned minimum,unsigned maximum,unsigned *length)
{
    uint32_t cci=0;efi_status status=get_cci(st,driver,&cci);
    if (status) return status;
    if (cci & (CCI_COMPLETE|CCI_BUSY|CCI_FAILURE)) {
        number(st,"Previous command not idle: ",cci);return EFI_UNSUPPORTED;
    }
    status=send_control(st,driver,command);
    if (!status) status=wait_cci(st,driver,CCI_COMPLETE,&cci);
    if (status) return status; /* Do not cancel/reset an unresolved command. */
    unsigned bytes=(cci>>8)&255;
    if (bytes<minimum || bytes>maximum || bytes>32) {
        number(st,"Unexpected response length: ",bytes);status=EFI_UNSUPPORTED;
    } else if (bytes) {
#ifdef UCSI_STATUS_FULL_BLOCK
        status=message_read(st,driver,data,bytes,cci);
#else
        status=guarded_read(st,driver,0x20200,data,bytes);
#endif
    }
    /* Acknowledge only command completion; leave connector-change events pending. */
    efi_status ack=send_control(st,driver,0x20004);
    if (!ack) ack=wait_cci(st,driver,CCI_ACK,&cci);
    if (!status && !ack) *length=bytes;
    return status ? status : ack;
}
static void raw_response(struct efi_system_table *st,const uint8_t *data,unsigned size)
{
    for (unsigned i=0;i<size;i+=8) {
        number(st,"  byte offset: ",i);
        number(st,"  raw: ",little(data+i,size-i<8 ? size-i : 8));
    }
}
static efi_status query_connectors(struct efi_system_table *st,uint8_t *driver)
{
    uint8_t data[32];unsigned length=0;uint32_t cci=0;
    efi_status status=get_cci(st,driver,&cci);
    if (status) return status;
    if (cci) {number(st,"Fresh-boot idle CCI required: ",cci);return EFI_UNSUPPORTED;}
    status=run_query(st,driver,0x10005,data,0,0,&length);
    if (!status) status=run_query(st,driver,6,data,16,16,&length);
    if (!status) {
        out(st,"GET_CAPABILITY\n");raw_response(st,data,length);
        unsigned count=data[4];number(st,"UCSI connector count: ",count);
        if (!count || count>2) status=EFI_UNSUPPORTED;
        for (unsigned port=1;!status && port<=count;port++) {
            number(st,"UCSI CONNECTOR: ",port);
            status=run_query(st,driver,((uint64_t)port<<16)|7,data,4,4,&length);
            if (status) break;
            out(st,"GET_CONNECTOR_CAPABILITY\n");raw_response(st,data,length);
            uint32_t caps=(uint32_t)little(data,4);
            number(st,"DFP/host capability: ",caps&1);
            number(st,"UFP/device capability: ",(caps>>1)&1);
            number(st,"Swap to DFP capability: ",(caps>>10)&1);
            number(st,"Swap to UFP capability: ",(caps>>11)&1);
            status=run_query(st,driver,((uint64_t)port<<16)|0x12,data,19,32,&length);
            if (status) break;
            out(st,"GET_CONNECTOR_STATUS\n");raw_response(st,data,length);
            uint32_t value=(uint32_t)little(data,4);unsigned partner=value>>29;
            number(st,"Connected: ",(value>>19)&1);
            number(st,"Power operation mode: ",(value>>16)&7);
            number(st,"Power direction (1=source): ",(value>>20)&1);
            number(st,"Partner type: ",partner);
            if (value&(1u<<19)) {
                if (partner==1) out(st,"Partner is DFP/host; local data role is device.\n");
                else if (partner==2 || partner==4) out(st,"Partner is UFP/device; local data role is host.\n");
            }
        }
    }
    /* On success disable the temporary notification mask. Do not depend on
     * further commands in Notifications Disabled; Windows initializes PPM next. */
    if (!status) {
        status=send_control(st,driver,5);
        if (!status) status=wait_cci(st,driver,CCI_COMPLETE,&cci);
        if (!status) out(st,"Temporary UCSI notifications disabled.\n");
    }
    return status;
}
#ifndef UCSI_STATUS_ENTRY
#define UCSI_STATUS_ENTRY efi_main
#endif
efi_status UCSI_STATUS_ENTRY(efi_handle image,struct efi_system_table *st)
{
    efi_status status=ucsi_register_preflight(image,st);
    if (status) return status;
    out(st,"q1n1 A16 UCSI connector query " UCSI_STATUS_VERSION "\n"
           "Temporarily enable command notifications; query capabilities/status.\n");
    uint8_t *driver=find_driver(st);
    if (!driver || !st->boot->stall) return EFI_UNSUPPORTED;
    status=st->boot->set_watchdog(30,0,0,NULL);
    if (status) return status;
    status=query_connectors(st,driver);
    efi_status watchdog=st->boot->set_watchdog(0,0,0,NULL);
    number(st,"Connector query EFI status: ",status);
    out(st,"No role, power, mux, reset, or cancellation command sent.\n"
           "GLINK cache freshness remains unproven; serial enumeration remains untested.\n");
    if (status) out(st,"Stopped without recovery commands. Boot Windows to reinitialize UCSI.\n");
    out(st,"Connector query finished. Returning to shell.\n");
    return status ? status : watchdog;
}

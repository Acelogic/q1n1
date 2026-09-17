/* SPDX-License-Identifier: MIT */
/* One standard, firmware-arbitrated SET_UOR for the physically mapped USB link.
 * Accepting incoming swaps and initiating an OPM request are separate UCSI controls.
 * A failure is recorded; no power-role, CC mode, cancellation, reset or retry is sent.
 */
#define UCSI_DATA_ROLE_REQUEST
#define UCSI_STATUS_ENTRY ucsi_status_query_main
#include "usb-ucsi-status-v2.c"
static int active_option(efi_handle image,struct efi_system_table *st)
{
    struct efi_loaded_image *self=NULL;
    if (st->boot->handle_protocol(image,&loaded_guid,(void **)&self) || !self ||
        !self->options || self->options_size>1024 || (self->options_size&1)) return 0;
    const char16 *s=self->options;unsigned count=self->options_size/2,found=0;
    for (unsigned n=0;n<count && s[n];) {
        while (n<count && (s[n]==' ' || s[n]=='\t')) n++;
        unsigned start=n;while(n<count && s[n] && s[n]!=' ' && s[n]!='\t') n++;
        if (start==n) break;
        if (s[start]!='-') continue;
        const char option[]="--device0";
        if (found || n-start!=sizeof(option)-1) return 0;
        for(unsigned i=0;i<sizeof(option)-1;i++) if(s[start+i]!=option[i]) return 0;
        found=1;
    }
    return found;
}
static efi_status acknowledge(struct efi_system_table *st,uint8_t *driver)
{
    uint32_t cci=0;efi_status status=send_control(st,driver,0x20004);
    return status ? status : wait_cci(st,driver,CCI_ACK,&cci);
}
static efi_status allow_serial(struct efi_system_table *st,uint8_t *driver)
{
    uint32_t cci=0;
    efi_status status=send_control(st,driver,5);
    if(!status) status=wait_cci(st,driver,CCI_COMPLETE,&cci);
    if(!status) out(st,"A16 UFP/device and partner DFP/host confirmed. Serial test may start.\n");
    return status;
}
static efi_status device0_request(struct efi_system_table *st,uint8_t *driver)
{
    uint32_t cci=0;uint8_t data[32];unsigned size=0;
    efi_status status=get_cci(st,driver,&cci);
    if(status || cci) return status ? status : EFI_UNSUPPORTED;
    status=run_query(st,driver,0x10005,data,0,0,&size);
    if(!status) status=run_query(st,driver,6,data,16,16,&size);
    if(status) return status;
    if(data[4]!=2 || !(little(data,4)&4)) return EFI_UNSUPPORTED;
    status=run_query(st,driver,0x10007,data,4,4,&size);
    if(status) return status;
    uint32_t caps=(uint32_t)little(data,4);
    number(st,"Connector 1 capability: ",caps);
    if((caps&7)!=7) return EFI_UNSUPPORTED;
    status=run_query(st,driver,0x10012,data,19,32,&size);
    if(status) return status;
    uint32_t before=(uint32_t)little(data,4);
    number(st,"Connector 1 status before: ",before);raw_response(st,data,size);
    /* Require a connected PD data link and local sink in both role states.
     * Connector 2 is never selected; its snapshot lacked the USB data flag. */
    if(!(before&(1u<<19)) || !(before&(1u<<21)) || ((before>>16)&7)!=3 ||
       ((before>>29)!=1 && (before>>29)!=2) || (before&(1u<<20))) {
        out(st,"Connected PD USB link differs from the mapped Mac link; no role request.\n");
        return EFI_UNSUPPORTED;
    }
    if((before>>29)==1) {
        out(st,"UCSI already reports the desired data roles; no role request.\n");
        return allow_serial(st,driver);
    }
    out(st,"Requesting one SET_UOR: connector 1 to UFP/device. Power role unchanged.\n");
    /* Match Linux ucsi_dr_swap: initiate UFP and accept incoming role swaps.
     * This changes the live connector policy, not nonvolatile firmware state. */
    out(st,"SET_UOR also enables incoming data-role swaps for this connection.\n");
    status=send_control(st,driver,0x3010009);
    if(!status) status=wait_cci(st,driver,CCI_COMPLETE,&cci);
    number(st,"Role command final CCI: ",cci);
    if(status) {
        if((cci&CCI_COMPLETE) && !(cci&CCI_BUSY)) {
            efi_status ack=acknowledge(st,driver);
            if(!ack) {
                /* UCSI 2.x errors are scoped to the failing command's connector.
                 * Selector zero reads the general error, not connector 1's error. */
                efi_status error=run_query(st,driver,0x10013,data,2,32,&size);
                number(st,"GET_ERROR_STATUS result: ",error);
                if(!error) {
                    uint16_t flags=(uint16_t)little(data,2);
                    number(st,"Connector 1 error flags: ",flags);
                    if(!flags) out(st,"Error flags are zero; rejection reason remains unresolved.\n");
                    out(st,"UCSI role error data\n");raw_response(st,data,size);
                }
            }
        }
        return status;
    }
    if((cci>>8)&255) return EFI_UNSUPPORTED;
    status=acknowledge(st,driver);
    if(!status) status=run_query(st,driver,0x10012,data,19,32,&size);
    if(status) return status;
    uint32_t after=(uint32_t)little(data,4);
    number(st,"Connector 1 status after: ",after);raw_response(st,data,size);
    if(!(after&(1u<<19)) || !(after&(1u<<21)) || ((after>>16)&7)!=3 ||
       (after>>29)!=1 || (after&(1u<<20))) {
        out(st,"UFP role not confirmed in connector status; serial launch refused.\n");return EFI_UNSUPPORTED;
    }
    return allow_serial(st,driver);
}
efi_status efi_main(efi_handle image,struct efi_system_table *st)
{
    if(!st || !st->boot || !st->console_out) return EFI_UNSUPPORTED;
    out(st,"q1n1 A16 UCSI data-role request v4\n");
    if(!active_option(image,st)) {out(st,"Use --device0 for the single connector-1 role request.\n");return EFI_UNSUPPORTED;}
    efi_status status=ucsi_register_preflight(image,st);
    if(status) return status;
    uint8_t *driver=find_driver(st);
    if(!driver || !st->boot->stall) return EFI_UNSUPPORTED;
    status=st->boot->set_watchdog(30,0,0,NULL);
    if(status) return status;
    status=device0_request(st,driver);
    efi_status watchdog=st->boot->set_watchdog(0,0,0,NULL);
    number(st,"Data-role experiment EFI status: ",status);
    out(st,"No power-role change, CC-mode change, reset or retry requested.\n");
    if(status) out(st,"Serial must not start. Boot Windows to reinitialize normal policy.\n");
    return status ? status : watchdog;
}

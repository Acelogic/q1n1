/* SPDX-License-Identifier: MIT */
/* Boot-services-only CDC ACM greeting/echo experiment for A16 BIOS312.
 * Firmware owns the controller and DMA. This app never exits boot services. */
#define Q1N1_ROLE_LIBRARY
#include "usb-role-change.c"
#include "usbfn.h"

#define DMA_SIZE 2048
#define NOT_READY EFI_ERROR(6)
#define DEVICE_ERROR EFI_ERROR(7)
#if !defined(Q1N1_CDC_HOST_TEST) && !defined(Q1N1_EXTERNAL_MEM)
void *memset(void *p, int v, size_t n) { uint8_t *b=p; while(n--) *b++=(uint8_t)v; return p; }
void *memcpy(void *d, const void *s, size_t n) { uint8_t *a=d; const uint8_t *b=s; while(n--) *a++=*b++; return d; }
#endif
static void copy_bytes(void *d, const void *s, unsigned n)
{ uint8_t *a=d; const uint8_t *b=s; while(n--) *a++=*b++; }

#ifndef Q1N1_CDC_PRODUCT
#define Q1N1_CDC_PRODUCT "q1n1 A16 serial"
#endif
#ifndef Q1N1_CDC_SERIAL
#define Q1N1_CDC_SERIAL "A16-Q1N1-UEFI"
#endif
/* Existing q1n1/m1n1 development identity; local experimental use only. */
static uint8_t dev2[] = {18,1,0,2,2,0,0,64, 0x09,0x12,0x6d,0x31, 0,1,1,2,3,1};
static uint8_t dev3[] = {18,1,0,3,2,0,0,9,  0x09,0x12,0x6d,0x31, 0,1,1,2,3,1};
static const uint8_t qualifier[] = {10,6,0,2,2,0,0,64,1,0};
static uint8_t config2[] = {
    9,2,67,0,2,1,0,0xc0,0,
    9,4,0,0,1,2,2,0,0,
    5,0x24,0,0x10,1, 5,0x24,1,0,1, 4,0x24,2,2, 5,0x24,6,0,1,
    7,5,0x81,3,16,0,9,
    9,4,1,0,2,0x0a,0,0,0,
    7,5,0x02,2,0,2,0, 7,5,0x82,2,0,2,0
};
static uint8_t config3[] = {
    9,2,85,0,2,1,0,0xc0,0,
    9,4,0,0,1,2,2,0,0,
    5,0x24,0,0x10,1, 5,0x24,1,0,1, 4,0x24,2,2, 5,0x24,6,0,1,
    7,5,0x81,3,16,0,9, 6,0x30,0,0,16,0,
    9,4,1,0,2,0x0a,0,0,0,
    7,5,0x02,2,0,4,0, 6,0x30,0,0,0,0,
    7,5,0x82,2,0,4,0, 6,0x30,0,0,0,0
};
static uint8_t bos[] = {5,15,22,0,2, 7,16,2,0,0,0,0, 10,16,3,0,14,0,1,0,0,0};
static void *eps_control[] = {config2+37}, *eps_data[] = {config2+53,config2+60};
static struct usb_interface_info if_control = {config2+9,eps_control}, if_data = {config2+44,eps_data};
static struct usb_interface_info *interfaces[] = {&if_control,&if_data};
static struct usb_config_info config_info = {config2,interfaces};
static struct usb_config_info *configs[] = {&config_info};
static struct usb_device_info device_info = {dev2,configs};
static struct usb_ss_endpoint ss_int = {config3+37,config3+44};
static struct usb_ss_endpoint ss_out = {config3+59,config3+66}, ss_in = {config3+72,config3+79};
static struct usb_ss_endpoint *ss_ep_control[] = {&ss_int}, *ss_ep_data[] = {&ss_out,&ss_in};
static struct usb_ss_interface ss_control = {config3+9,ss_ep_control}, ss_data = {config3+50,ss_ep_data};
static struct usb_ss_interface *ss_interfaces[] = {&ss_control,&ss_data};
static struct usb_ss_config ss_config = {config3,ss_interfaces};
static struct usb_ss_config *ss_configs[] = {&ss_config};
static struct usb_ss_device ss_device = {dev3,ss_configs,bos};
_Static_assert(sizeof(config2)==67 && sizeof(config3)==85 && sizeof(bos)==22, "Descriptor lengths");

enum { CTL_IDLE, CTL_TX_DATA, CTL_RX_DATA, CTL_TX_STATUS, CTL_RX_STATUS };
struct cdc {
    struct usbfn *fn;
    uint8_t *control, *rx, *tx, *notification;
    uint32_t speed, control_stage, out_kind;
    unsigned configured, lines, rx_busy, tx_busy, notify_busy, notify_dirty;
    unsigned greeting, echo_bytes, rx_bytes, tx_bytes;
    uint8_t line_coding[7];
    uint64_t received, transmitted, setups, resets;
};
static efi_status submit(struct cdc *c, uint8_t ep, uint32_t direction, void *buffer, uint64_t bytes)
{
    uint64_t accepted = bytes;
    efi_status s = c->fn->transfer(c->fn,ep,direction,&accepted,buffer);
    return s ? s : accepted == bytes ? 0 : DEVICE_ERROR;
}
static efi_status ctl_transfer(struct cdc *c, unsigned stage, unsigned bytes)
{
    uint32_t dir = stage==CTL_TX_DATA || stage==CTL_TX_STATUS ? USB_IN : USB_OUT;
    c->control_stage=stage;
    return submit(c,0,dir,c->control,bytes);
}
static efi_status stall_control(struct cdc *c)
{
    c->control_stage=CTL_IDLE;
    efi_status a=c->fn->set_stall(c->fn,0,USB_OUT,1);
    efi_status b=c->fn->set_stall(c->fn,0,USB_IN,1);
    return a ? a : b;
}
static void reset_state(struct cdc *c)
{
    c->configured=c->lines=c->rx_busy=c->tx_busy=c->notify_busy=c->notify_dirty=0;
    c->control_stage=CTL_IDLE; c->echo_bytes=c->tx_bytes=0; c->greeting=1;
}
static unsigned string_descriptor(uint8_t *b, unsigned index)
{
    if (!index) { b[0]=4;b[1]=3;b[2]=9;b[3]=4;return 4; }
    const char *s=index==1 ? "q1n1" : index==2 ? Q1N1_CDC_PRODUCT : index==3 ? Q1N1_CDC_SERIAL : NULL;
    if (!s) return 0;
    unsigned n=2; b[1]=3;
    while (*s && n<126) { b[n++]=(uint8_t)*s++;b[n++]=0; }
    b[0]=(uint8_t)n;return n;
}
static efi_status descriptor(struct cdc *c, const struct usb_setup *r)
{
    unsigned type=r->value>>8,index=r->value&255,n=0;
    const uint8_t *src=NULL;
    if (type!=3 && (index || r->index)) return stall_control(c);
    switch(type) {
    case 1: src=c->speed==USB_SUPER ? dev3 : dev2; n=18;break;
    case 2: src=c->speed==USB_SUPER ? config3 : config2; n=c->speed==USB_SUPER ? sizeof(config3) : sizeof(config2);break;
    case 3:
        if (r->index && r->index!=0x409) return stall_control(c);
        n=string_descriptor(c->control,index);break;
    case 6: if(c->speed==USB_SUPER)return stall_control(c);src=qualifier;n=sizeof(qualifier);break;
    case 7:
        if(c->speed==USB_SUPER)return stall_control(c);
        copy_bytes(c->control,config2,sizeof(config2));c->control[1]=7;
        c->control[57]=c->control[64]=c->speed==USB_HIGH ? 64 : 0;
        c->control[58]=c->control[65]=c->speed==USB_HIGH ? 0 : 2;
        n=sizeof(config2);break;
    case 15: src=bos;n=sizeof(bos);break;
    default: return stall_control(c);
    }
    if(!n || !r->length)return stall_control(c);
    if(n>r->length)n=r->length;
    if(src)copy_bytes(c->control,src,n);
    return ctl_transfer(c,CTL_TX_DATA,n);
}
static int valid_endpoint(uint16_t ep)
{ return ep==0 || ep==0x80 || ep==0x81 || ep==2 || ep==0x82; }
static efi_status setup(struct cdc *c, const struct usb_setup *r)
{
    c->setups++;
    if(c->control_stage!=CTL_IDLE) {
        c->fn->abort(c->fn,0,USB_OUT);c->fn->abort(c->fn,0,USB_IN);
    }
    c->control_stage=CTL_IDLE;
    if(r->type==0x80 && r->request==6)return descriptor(c,r);
    if(r->type==0 && r->request==5 && !r->index && !r->length && r->value<=127)
        return ctl_transfer(c,CTL_TX_STATUS,0); /* Controller applies SET_ADDRESS. */
    if(r->type==0 && r->request==9 && !r->index && !r->length && r->value<=1) {
        c->fn->abort(c->fn,2,USB_OUT);c->fn->abort(c->fn,2,USB_IN);c->fn->abort(c->fn,1,USB_IN);
        reset_state(c);c->configured=r->value;c->notify_dirty=1;
        return ctl_transfer(c,CTL_TX_STATUS,0);
    }
    if(r->type==0x80 && r->request==8 && !r->value && !r->index && r->length==1) {
        c->control[0]=(uint8_t)c->configured;return ctl_transfer(c,CTL_TX_DATA,1);
    }
    if((r->type==0x80 || r->type==0x81 || r->type==0x82) && !r->request && !r->value && r->length==2) {
        uint8_t state=0;
        if(r->type==0x80) { if(r->index)return stall_control(c);state=1; }
        else if(r->type==0x81) { if(r->index>1)return stall_control(c); }
        else {
            if(!valid_endpoint(r->index))return stall_control(c);
            efi_status s=c->fn->get_stall(c->fn,r->index&15,r->index>>7,&state);if(s)return s;
        }
        c->control[0]=state;c->control[1]=0;return ctl_transfer(c,CTL_TX_DATA,2);
    }
    if(r->type==2 && (r->request==1 || r->request==3) && !r->value && !r->length && valid_endpoint(r->index) && (r->index&15)) {
        efi_status s=c->fn->set_stall(c->fn,r->index&15,r->index>>7,r->request==3);
        return s ? s : ctl_transfer(c,CTL_TX_STATUS,0);
    }
    if(r->type==0x81 && r->request==10 && !r->value && r->index<=1 && r->length==1) {
        c->control[0]=0;return ctl_transfer(c,CTL_TX_DATA,1);
    }
    if(r->type==1 && r->request==11 && !r->value && r->index<=1 && !r->length)
        return ctl_transfer(c,CTL_TX_STATUS,0);
    if(r->type==0x21 && r->request==0x20 && !r->value && !r->index && r->length==7) {
        c->out_kind=1;return ctl_transfer(c,CTL_RX_DATA,7);
    }
    if(r->type==0xa1 && r->request==0x21 && !r->value && !r->index && r->length==7) {
        copy_bytes(c->control,c->line_coding,7);return ctl_transfer(c,CTL_TX_DATA,7);
    }
    if(r->type==0x21 && r->request==0x22 && !r->index && !r->length && !(r->value&~3)) {
        c->lines=r->value;c->notify_dirty=1;return ctl_transfer(c,CTL_TX_STATUS,0);
    }
    if(c->speed==USB_SUPER && r->type==0 && r->request==0x30 && !r->value && !r->index && r->length==6) {
        c->out_kind=2;return ctl_transfer(c,CTL_RX_DATA,6);
    }
    if(c->speed==USB_SUPER && r->type==0 && r->request==0x31 && !r->index && !r->length)
        return ctl_transfer(c,CTL_TX_STATUS,0);
    return stall_control(c);
}
static efi_status completed(struct cdc *c, const struct usb_transfer_result *r)
{
    if(r->status!=USB_COMPLETE && r->status!=USB_ABORTED)return 0;
    if(r->endpoint==0) {
        if(r->buffer!=c->control)return DEVICE_ERROR;
        if(r->status==USB_ABORTED)return 0; /* A newer SETUP can cancel the old request. */
        if(c->control_stage==CTL_TX_DATA && r->direction==USB_IN)return ctl_transfer(c,CTL_RX_STATUS,0);
        if(c->control_stage==CTL_RX_DATA && r->direction==USB_OUT) {
            if(r->bytes!=(c->out_kind==1 ? 7u : 6u))return stall_control(c);
            if(c->out_kind==1)copy_bytes(c->line_coding,c->control,7);
            return ctl_transfer(c,CTL_TX_STATUS,0);
        }
        if((c->control_stage==CTL_TX_STATUS && r->direction==USB_IN) ||
           (c->control_stage==CTL_RX_STATUS && r->direction==USB_OUT))c->control_stage=CTL_IDLE;
        return 0;
    }
    if(!c->configured)return 0;
    if(r->endpoint==1 && r->direction==USB_IN && r->buffer==c->notification) { c->notify_busy=0;return 0; }
    if(r->endpoint!=2)return DEVICE_ERROR;
    if(r->direction==USB_OUT) {
        if(r->buffer!=c->rx || r->bytes>c->rx_bytes || r->bytes>DMA_SIZE)return DEVICE_ERROR;
        c->rx_busy=0;
        if(r->status==USB_COMPLETE) { c->echo_bytes=(unsigned)r->bytes;c->received+=r->bytes; }
    } else {
        if(r->buffer!=c->tx)return DEVICE_ERROR;
        c->tx_busy=0;
        if(r->status==USB_COMPLETE) {
            if(r->bytes!=c->tx_bytes)return DEVICE_ERROR;
            c->transmitted+=r->bytes;
            unsigned packet=c->speed==USB_SUPER ? 1024 : c->speed==USB_HIGH ? 512 : 64;
            if(c->tx_bytes && !(c->tx_bytes%packet)) {
                /* End a packet-aligned echo so host reads can complete. */
                c->tx_bytes=0;
                efi_status s=submit(c,2,USB_IN,c->tx,0);
                if(!s)c->tx_busy=1;
                return s;
            }
        }
        c->tx_bytes=0;
    }
    return 0;
}
static efi_status service(struct cdc *c)
{
    if(!c->configured || c->control_stage!=CTL_IDLE)return 0;
    efi_status s;
    if(!c->rx_busy && !c->echo_bytes) {
        /* One packet per receive: a host write exactly one packet long must
         * complete even when the host does not append an OUT ZLP. */
        unsigned n=c->speed==USB_SUPER ? 1024 : c->speed==USB_HIGH ? 512 : 64;
        s=submit(c,2,USB_OUT,c->rx,n);
        if(!s) { c->rx_busy=1;c->rx_bytes=n; }else if(s!=NOT_READY)return s;
    }
    if(!c->tx_busy && (c->lines&1)) {
        static const char hello[]="hello from q1n1 on A16 (UEFI)\r\n";
        unsigned n=c->greeting ? sizeof(hello)-1 : c->echo_bytes;
        if(n) {
            copy_bytes(c->tx,c->greeting ? (const void *)hello : c->rx,n);
            s=submit(c,2,USB_IN,c->tx,n);
            if(!s) { c->tx_busy=1;c->tx_bytes=n;if(c->greeting)c->greeting=0;else c->echo_bytes=0; }
            else if(s!=NOT_READY)return s;
        }
    }
    if(!c->notify_busy && c->notify_dirty) {
        const uint8_t notification[]={0xa1,0x20,0,0,0,0,2,0,(c->lines&1)?3:0,0};
        copy_bytes(c->notification,notification,sizeof(notification));
        s=submit(c,1,USB_IN,c->notification,sizeof(notification));
        if(!s) { c->notify_busy=1;c->notify_dirty=0; }else if(s!=NOT_READY)return s;
    }
    return 0;
}
static efi_status event_step(struct cdc *c, uint32_t event, union usb_payload *p, uint64_t size)
{
    switch(event) {
    case USB_SETUP: return size<sizeof(p->setup) ? DEVICE_ERROR : setup(c,&p->setup);
    case USB_RX: case USB_TX: return size<sizeof(p->transfer) ? DEVICE_ERROR : completed(c,&p->transfer);
    case USB_RESET: c->resets++;reset_state(c);break;
    case USB_DETACH: reset_state(c);break;
    case USB_SPEED:
        if(size<4 || p->speed<USB_FULL || p->speed>USB_SUPER)return EFI_UNSUPPORTED;
        c->speed=p->speed;
        config2[57]=config2[64]=c->speed==USB_FULL ? 64 : 0;
        config2[58]=config2[65]=c->speed==USB_FULL ? 0 : 2;
        break;
    case USB_NONE: case USB_ATTACH: case USB_SUSPEND: case USB_RESUME: break;
    default: return EFI_UNSUPPORTED;
    }
    return 0;
}
static struct usbfn *checked_usbfn(struct efi_system_table *st)
{
    uint8_t *base=find_firmware(st,0xb000,0x1000,0x7400,UINT64_C(0x30f3db4034710f10));
    if(!base)return NULL;
    struct usbfn *fn=NULL;
    if(st->boot->locate_protocol(&function_guid,NULL,(void **)&fn) || !fn || fn->revision!=0x10003)return NULL;
    const uintptr_t *methods=(const uintptr_t *)((const uint8_t *)fn+8);
    static const uint32_t rvas[]={0x2e10,0x2e70,0x31e0,0x3218,0x3600,0x365c,0x367c,0x36dc,
        0x36ec,0x38d4,0x3978,0x399c,0x39f0,0x3a04,0x3a08,0x3a2c,0x3a74,0x3b58,0x3bc4};
    for(unsigned n=0;n<sizeof(rvas)/sizeof(rvas[0]);n++)if(methods[n]!=(uintptr_t)(base+rvas[n]))return NULL;
    return fn;
}
#if !defined(Q1N1_CDC_HOST_TEST) && !defined(Q1N1_CDC_LIBRARY)
static uint64_t ticks(void) { uint64_t v;__asm__ volatile("mrs %0, cntpct_el0":"=r"(v));return v; }
efi_status efi_main(efi_handle image, struct efi_system_table *st)
{
    if(!st || !st->boot || !st->console_out)return EFI_UNSUPPORTED;
#ifdef Q1N1_ALLOW_INACTIVE_USB0
    out(st,"q1n1 A16 UEFI USB serial echo v3 (inactive USB0 startup)\n");
#else
    out(st,"q1n1 A16 UEFI USB serial echo v2\n");
#endif
    struct efi_loaded_image *self=NULL;
    if(st->boot->handle_protocol(image,&loaded_guid,(void **)&self) || !self)return EFI_UNSUPPORTED;
    if(device_option(self)!=1) { out(st,"Use --device0 to start this five-minute serial experiment.\n");return 0; }
    efi_status status=prepare_usb0(image,st,1);
    if(status)return status;
    struct cdc c={0};
    c.fn=checked_usbfn(st);
    if(!c.fn) { out(st,"REFUSED: USB Function driver code or ABI differs from BIOS312.\n");return EFI_UNSUPPORTED; }
    out(st,"USB Function driver fingerprint and callback ABI matched.\n");
    c.speed=USB_HIGH;c.greeting=1;
    const uint8_t line[]={0,0xc2,1,0,0,0,8};copy_bytes(c.line_coding,line,7);
    uint8_t **buffers[]={&c.control,&c.rx,&c.tx,&c.notification};
    unsigned allocated=0;
    status=st->boot->set_watchdog(360,0,0,NULL);
    int watchdog=!status;
    for(;!status && allocated<4;allocated++) {
        status=c.fn->allocate(c.fn,DMA_SIZE,(void **)buffers[allocated]);
        if(status)break;
    }
    int started=0;
    if(!status) { started=1;status=c.fn->start(c.fn); }
    if(!status)status=c.fn->configure_ex(c.fn,&device_info,&ss_device);
    number(st,"USBFn initialization status: ",status);
    if(!status) {
        out(st,"CDC ACM descriptors active: 1209:316D / A16-Q1N1-UEFI\n"
               "Waiting for the Mac. This test stays in UEFI; ESC stops it.\n");
        uint64_t frequency;__asm__ volatile("mrs %0, cntfrq_el0":"=r"(frequency));
        uint64_t start=ticks();
        unsigned previous_config=0,previous_lines=0;
        efi_status (*stall)(uint64_t)=(efi_status (*)(uint64_t))st->boot->stall;
        while(frequency && ticks()-start<frequency*300) {
            union usb_payload payload={0};uint64_t size=sizeof(payload);uint32_t event=0;
            status=c.fn->event(c.fn,&event,&size,&payload);
            if(status==NOT_READY) { status=0;event=USB_NONE; }
            if(status)break;
            if(event==USB_SPEED)number(st,"USB bus speed (2 full, 3 high, 4 super): ",payload.speed);
            if(event==USB_ATTACH)out(st,"USB attach.\n");
            if(event==USB_RESET)out(st,"USB reset.\n");
            if(event==USB_DETACH)out(st,"USB detach.\n");
            status=event_step(&c,event,&payload,size);if(status)break;
            if(c.configured!=previous_config) { number(st,"USB configuration: ",c.configured);previous_config=c.configured; }
            if(c.lines!=previous_lines) { number(st,"CDC control lines: ",c.lines);previous_lines=c.lines; }
            status=service(&c);if(status)break;
            struct { uint16_t scan,unicode; } key;
            efi_status (*read_key)(struct efi_input *,void *)=st->console_in ?
                (efi_status (*)(struct efi_input *,void *))st->console_in->read_key : NULL;
            if(read_key && !read_key(st->console_in,&key) && (key.scan==23 || key.unicode==27))break;
            if(event==USB_NONE)stall(1000);
        }
    }
    number(st,"Serial test status: ",status);
    number(st,"Setup requests: ",c.setups);number(st,"Bulk RX bytes: ",c.received);number(st,"Bulk TX bytes: ",c.transmitted);
    if(started) {
        efi_status stopped=c.fn->stop(c.fn);
        number(st,"USBFn stop status: ",stopped);
        if(stopped) {
            out(st,"STOP FAILED. Keeping DMA buffers and this app resident. Power-cycle the A16.\n");
            for(;;) __asm__ volatile("wfe");
        }
    }
    for(unsigned n=0;n<allocated;n++)c.fn->free(c.fn,*buffers[n]);
    if(watchdog)st->boot->set_watchdog(0,0,0,NULL);
    out(st,"Serial test ended. Returning to shell; post-EBS serial is still pending.\n");
    return status;
}
#endif

/* SPDX-License-Identifier: MIT */
/* Native USB host simulation. No controller access or firmware execution. */
#include <assert.h>
#include <stdio.h>
#include <string.h>
#define Q1N1_CDC_HOST_TEST
#include "../platform/uefi/usb-serial.c"

struct pending { void *buffer; uint64_t bytes; unsigned active; };
static struct pending pending[3][2];
static unsigned stalls, partial, unavailable, checks;
static uint8_t buffers[4][DMA_SIZE];
static efi_status transfer(struct usbfn *f,uint8_t ep,uint32_t dir,uint64_t *n,void *p)
{
    (void)f;assert(ep<3 && dir<2 && *n<=DMA_SIZE);
    if(unavailable)return NOT_READY;
    assert(!pending[ep][dir].active);
    if(partial && *n)(*n)--;
    pending[ep][dir]=(struct pending){p,*n,1};return 0;
}
static efi_status abort_mock(struct usbfn *f,uint8_t ep,uint32_t dir)
{ (void)f;pending[ep][dir].active=0;return 0; }
static efi_status set_stall_mock(struct usbfn *f,uint8_t ep,uint32_t dir,uint8_t value)
{ (void)f;(void)ep;(void)dir;stalls+=value;return 0; }
static efi_status get_stall_mock(struct usbfn *f,uint8_t ep,uint32_t dir,uint8_t *value)
{ (void)f;(void)ep;(void)dir;*value=0;return 0; }
static struct usbfn mock={.transfer=transfer,.abort=abort_mock,.set_stall=set_stall_mock,.get_stall=get_stall_mock};
static struct cdc c;
static void fresh(void)
{
    memset(&c,0,sizeof(c));memset(pending,0,sizeof(pending));memset(buffers,0,sizeof(buffers));
    stalls=partial=unavailable=0;c.fn=&mock;c.control=buffers[0];c.rx=buffers[1];c.tx=buffers[2];c.notification=buffers[3];
    c.speed=USB_HIGH;reset_state(&c);
}
static void request(uint8_t type,uint8_t code,uint16_t value,uint16_t index,uint16_t length)
{
    struct usb_setup r={type,code,value,index,length};assert(!setup(&c,&r));checks++;
}
static efi_status finish(uint8_t ep,uint32_t dir,uint64_t bytes)
{
    struct pending p=pending[ep][dir];assert(p.active && bytes<=p.bytes);pending[ep][dir].active=0;
    struct usb_transfer_result r={.bytes=bytes,.status=USB_COMPLETE,.endpoint=ep,.direction=dir,.buffer=p.buffer};
    return completed(&c,&r);
}
static void finish_control(void)
{
    unsigned old=c.control_stage;
    if(old==CTL_TX_DATA)assert(!finish(0,USB_IN,pending[0][USB_IN].bytes));
    if(c.control_stage==CTL_RX_STATUS)assert(!finish(0,USB_OUT,0));
    else if(c.control_stage==CTL_TX_STATUS)assert(!finish(0,USB_IN,0));
    assert(c.control_stage==CTL_IDLE);
}
static void descriptor_walk(const uint8_t *p,unsigned n,int super)
{
    unsigned off=0,interfaces_seen=0,endpoints=0,companions=0;
    assert((unsigned)(p[2]|p[3]<<8)==n && p[4]==2);
    while(off<n) {
        unsigned len=p[off];assert(len>=2 && off+len<=n);
        if(p[off+1]==4)interfaces_seen++;
        if(p[off+1]==5) { endpoints++;assert(len==7);if(super)assert(p[off+8]==0x30); }
        if(p[off+1]==0x30)companions++;
        off+=len;
    }
    assert(off==n && interfaces_seen==2 && endpoints==3 && companions==(super?3u:0u));checks++;
}
int main(void)
{
    fresh();descriptor_walk(config2,sizeof(config2),0);descriptor_walk(config3,sizeof(config3),1);
    for(unsigned speed=USB_FULL;speed<=USB_SUPER;speed++) {
        union usb_payload p={.speed=speed};assert(!event_step(&c,USB_SPEED,&p,4));
        request(0x80,6,0x100,0,64);assert(c.control[0]==18 && c.control[7]==(speed==USB_SUPER?9:64));finish_control();
        request(0x80,6,0x200,0,9);assert(pending[0][USB_IN].bytes==9);finish_control();
        request(0x80,6,0x200,0,255);assert(pending[0][USB_IN].bytes==(speed==USB_SUPER?85:67));
        unsigned packet_offset=speed==USB_SUPER?63:57;
        assert((unsigned)(c.control[packet_offset]|c.control[packet_offset+1]<<8)==(speed==USB_SUPER?1024u:speed==USB_HIGH?512u:64u));finish_control();
        request(0x80,6,0x303,0x409,255);assert(c.control[0]==28 && c.control[2]=='A' && c.control[3]==0);finish_control();
        if(speed!=USB_SUPER) { request(0x80,6,0x700,0,255);assert(c.control[1]==7 && c.control[57]==(speed==USB_HIGH?64:0));finish_control(); }
        else { request(0,0x30,0,0,6);assert(!finish(0,USB_OUT,6));finish_control(); }
    }
    request(0x80,6,0x30f,0x409,255);assert(stalls==2);
    request(0,5,12,0,0);finish_control();
    request(0,9,1,0,0);assert(c.configured && !service(&c) && !c.rx_busy);finish_control();
    request(0x80,8,0,0,1);assert(c.control[0]==1);finish_control();
    request(0x21,0x20,0,0,7);
    const uint8_t line[]={0,0xc2,1,0,0,0,8};memcpy(c.control,line,7);assert(!finish(0,USB_OUT,7));finish_control();
    request(0xa1,0x21,0,0,7);assert(!memcmp(c.control,line,7));finish_control();
    request(0x21,0x22,3,0,0);finish_control();assert(!service(&c));
    assert(c.rx_busy && c.tx_busy && c.notify_busy && c.notification[8]==3);
    assert(!memcmp(c.tx,"hello from q1n1",15));assert(!finish(1,USB_IN,10));
    /* RX may finish while the greeting is still in flight. Keep both buffers. */
    const uint8_t probe[]={0,1,2,0xff,'A','\r','\n'};memcpy(c.rx,probe,sizeof(probe));
    assert(!finish(2,USB_OUT,sizeof(probe)));assert(!service(&c));assert(c.echo_bytes==sizeof(probe) && !c.rx_busy);
    assert(!finish(2,USB_IN,c.tx_bytes));assert(!service(&c));assert(!memcmp(c.tx,probe,sizeof(probe)));
    assert(!service(&c));assert(c.rx_busy); /* Separate RX can proceed while TX owns its copy. */
    assert(!finish(2,USB_IN,sizeof(probe)));assert(c.received==sizeof(probe));checks++;
    /* Aligned echoes need a terminating ZLP, with buffer ownership retained. */
    memset(c.rx,0x5a,1024);assert(!finish(2,USB_OUT,1024));assert(!service(&c));
    assert(!finish(2,USB_IN,1024));assert(c.tx_busy && !c.tx_bytes && pending[2][USB_IN].active);
    assert(!finish(2,USB_IN,0));assert(!c.tx_busy);checks++;
    /* A new SETUP supersedes a control read; malformed packets cannot overflow. */
    request(0x80,6,0x100,0,18);request(0x80,6,0x200,0,9);finish_control();
    union usb_payload p={0};assert(event_step(&c,USB_SETUP,&p,7)==DEVICE_ERROR);
    assert(event_step(&c,USB_TX,&p,31)==DEVICE_ERROR);p.speed=1;assert(event_step(&c,USB_SPEED,&p,4)==EFI_UNSUPPORTED);checks++;
    p.transfer=(struct usb_transfer_result){.bytes=DMA_SIZE+1,.status=USB_COMPLETE,.endpoint=2,.direction=USB_OUT,.buffer=c.rx};
    assert(completed(&c,&p.transfer)==DEVICE_ERROR);checks++;
    fresh();c.configured=1;c.lines=1;unavailable=1;assert(!service(&c) && !c.tx_busy && !c.rx_busy);
    unavailable=0;partial=1;assert(service(&c)==DEVICE_ERROR);checks++;
    fresh();c.configured=1;c.lines=1;assert(!service(&c));assert(finish(2,USB_IN,1)==DEVICE_ERROR);checks++;
    fresh();c.configured=1;c.lines=3;c.echo_bytes=2;assert(!event_step(&c,USB_RESET,&p,0));assert(!c.configured && !c.lines && !c.echo_bytes && c.resets==1);checks++;
    for(unsigned speed=USB_FULL;speed<=USB_SUPER;speed++) {
        fresh();c.speed=speed;c.configured=1;c.lines=1;c.greeting=0;
        unsigned packet=speed==USB_FULL?64:speed==USB_HIGH?512:1024;
        assert(!service(&c) && pending[2][USB_OUT].bytes==packet);
        memset(c.rx,0xa5,packet);assert(!finish(2,USB_OUT,packet));
        assert(!service(&c) && pending[2][USB_IN].bytes==packet);
        assert(!finish(2,USB_IN,packet) && c.tx_busy && !pending[2][USB_IN].bytes);
        assert(!finish(2,USB_IN,0) && !c.tx_busy);checks++;
    }
    printf("PASS: %u CDC request/descriptor/data checks (simulated host only)\n",checks);
    return 0;
}

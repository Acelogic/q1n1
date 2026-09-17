/* SPDX-License-Identifier: MIT */
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#define Q1N1_UCSI_REG_HOST_TEST
#include "../platform/uefi/usb-ucsi-reg-probe.c"
static uint8_t original[GLINK_SIZE],bios[GLINK_SIZE] __attribute__((aligned(8)));
static struct efi_loaded_image li;
static struct efi_boot_services boot;
static struct efi_text console;
static struct efi_system_table st;
static efi_handle images[]={(void *)1,(void *)2},protocols[]={(void *)3,(void *)4};
static unsigned image_count,protocol_count,reads,arms,disarms,cases;
static int bad_arm,bad_pointer,overflow,underflow,no_copy,all_zero;
static efi_status read_status;
static unsigned fail_read,remote_error,drop_link;
static char log_data[8000];static size_t used;
static int same(efi_guid *a,efi_guid *b) { return !memcmp(a,b,sizeof(*a)); }
static efi_status output(struct efi_text *c,const char16 *s)
{
    (void)c;while (*s && used+1<sizeof(log_data)) log_data[used++]=(char)*s++;log_data[used]=0;return 0;
}
static efi_status locate(uint32_t kind,efi_guid *g,void *key,uint64_t *n,efi_handle **h)
{
    assert(kind==EFI_BY_PROTOCOL && !key);
    if (same(g,&loaded_guid)) { *n=image_count;*h=images;return 0; }
    assert(same(g,&glink_guid));if (!protocol_count) return EFI_NOT_FOUND;
    *n=protocol_count;*h=protocols;return 0;
}
static efi_status protocol(efi_handle h,efi_guid *g,void **p)
{
    (void)h;
    if (same(g,&loaded_guid)) *p=&li;
    else { assert(same(g,&glink_guid));*p=bad_pointer ? (void *)1 : bios+GLINK_PROTOCOL_RVA; }
    return 0;
}
static efi_status free_pool(void *p) { (void)p;return 0; }
static efi_status watchdog(uint64_t seconds,uint64_t code,uint64_t size,char16 *data)
{
    assert(!code && !size && !data);
    if (seconds) { assert(seconds==30);arms++;return bad_arm ? EFI_UNSUPPORTED : 0; }
    disarms++;return 0;
}
efi_status mock_register_read(uint32_t reg,uint8_t *buffer,uint32_t size)
{
    assert(size==4 && arms==1 && !disarms);
    assert(reg==(reads==0 ? 0x20100U : 0x20104U));reads++;
    if (fail_read && reads==fail_read) return EFI_UNSUPPORTED;
    if (!read_status && !no_copy) {
        uint32_t value=all_zero ? 0 : (reg==0x20100 ? 0x210 : 0x20000000);
        memcpy(buffer,&value,4);
        if (overflow) buffer[4]=0;
        if (underflow) buffer[-1]=0;
    }
    if (drop_link) bios[0x43e1]=0;
    if (remote_error) bios[0x4950]=1;
    return read_status;
}
static void reset(void)
{
    memcpy(bios,original,sizeof(bios));
    for (unsigned i=0;i<sizeof(callback_rvas)/sizeof(callback_rvas[0]);i++) *(uintptr_t *)(bios+GLINK_PROTOCOL_RVA+8+i*8)=(uintptr_t)(bios+callback_rvas[i]);
    *(uintptr_t *)(bios+0x4238)=(uintptr_t)&boot;bios[0x43e1]=1;
    li.image_base=bios;li.image_size=GLINK_SIZE;
    image_count=protocol_count=1;reads=arms=disarms=0;bad_arm=bad_pointer=0;read_status=0;
    overflow=underflow=no_copy=all_zero=0;fail_read=remote_error=drop_link=0;
    used=0;log_data[0]=0;
}
static void check(efi_status expected,unsigned expected_reads,const char *needle)
{
    efi_status status=efi_main((void *)1,&st);
    if (status!=expected || reads!=expected_reads || !strstr(log_data,needle)) {
        fprintf(stderr,"%s\nExpected: %s\n",log_data,needle);abort();
    }
    if (reads) assert(disarms==1);
    cases++;
}
int main(int argc,char **argv)
{
    assert(argc==2);FILE *f=fopen(argv[1],"rb");assert(f);
    assert(fread(original,1,sizeof(original),f)==sizeof(original));fclose(f);
    assert(hash_code(original)==GLINK_CODE_FNV);
    boot.locate_handle_buffer=locate;boot.handle_protocol=protocol;boot.free_pool=free_pool;boot.set_watchdog=watchdog;
    console.output=output;st.boot=&boot;st.console_out=&console;
    reset();check(0,2,"UCSI VERSION register: 0x0000000000000210");
    reset();check(0,2,"UCSI CCI register: 0x0000000020000000");
    reset();protocol_count=2;check(0,2,"Register probe complete");
    reset();image_count=0;check(EFI_UNSUPPORTED,0,"Matching BIOS312");
    reset();image_count=2;check(EFI_UNSUPPORTED,0,"Matching BIOS312");
    reset();bios[0x1000]^=1;check(EFI_UNSUPPORTED,0,"Matching BIOS312");
    reset();protocol_count=0;check(EFI_UNSUPPORTED,0,"protocol absent");
    reset();bad_pointer=1;check(EFI_UNSUPPORTED,0,"differs from inspected");
    reset();bios[GLINK_PROTOCOL_RVA]=0x0b;check(EFI_UNSUPPORTED,0,"revision mismatch");
    reset();*(uintptr_t *)(bios+GLINK_PROTOCOL_RVA+8)=1;check(EFI_UNSUPPORTED,0,"callback layout");
    reset();*(uintptr_t *)(bios+0x4178)=1;check(EFI_UNSUPPORTED,0,"callback layout");
    reset();*(uintptr_t *)(bios+0x4198)=1;check(EFI_UNSUPPORTED,0,"callback layout");
    reset();bios[0x43e1]=0;check(EFI_UNSUPPORTED,0,"not ready");
    reset();*(uintptr_t *)(bios+0x4238)=1;check(EFI_UNSUPPORTED,0,"not ready");
    reset();bad_arm=1;check(EFI_UNSUPPORTED,0,"Could not arm");
    reset();read_status=EFI_UNSUPPORTED;check(EFI_UNSUPPORTED,1,"probe incomplete");
    reset();overflow=1;check(EFI_UNSUPPORTED,1,"guard failed");
    reset();underflow=1;check(EFI_UNSUPPORTED,1,"guard failed");
    reset();no_copy=1;check(EFI_UNSUPPORTED,1,"not populated");
    reset();all_zero=1;check(EFI_UNSUPPORTED,1,"VERSION differs");
    reset();fail_read=2;check(EFI_UNSUPPORTED,2,"probe incomplete");
    reset();remote_error=1;check(EFI_UNSUPPORTED,1,"Remote status was not success");
    reset();drop_link=1;check(EFI_UNSUPPORTED,1,"GLINK became unavailable");
    reset();assert(request_register(bios,0x20108,bios)==EFI_UNSUPPORTED && reads==0);cases++;
    printf("PASS: %u " PROBE_NAME " read/guard cases; captured firmware never executed\n",cases);
    return 0;
}

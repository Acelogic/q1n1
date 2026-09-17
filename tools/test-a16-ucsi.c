/* SPDX-License-Identifier: MIT */
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#define Q1N1_UCSI_HOST_TEST
#include "../platform/uefi/usb-ucsi-probe.c"
static uint8_t original[GLINK_SIZE],bios[GLINK_SIZE] __attribute__((aligned(8)));
static struct efi_loaded_image li;
static struct efi_boot_services boot;
static struct efi_text console;
static struct efi_system_table st;
static efi_handle images[]={(void *)1,(void *)2},protocols[]={(void *)3,(void *)4};
static unsigned image_count,protocol_count,reads,arms,disarms,cases;
static int bad_arm,bad_pointer,bad_buffer,overflow,underflow,no_copy,all_zero;
static efi_status read_status;
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
efi_status mock_ucsi_read(uint8_t **buffer,uint8_t size)
{
    assert(size==GLINK_READ_SIZE && arms==1 && !disarms);reads++;
    if (bad_buffer) { *buffer=NULL;return 0; }
    if (!read_status && !no_copy) {
        memset(*buffer,0,size);
        if (!all_zero) { (*buffer)[1]=2;(*buffer)[7]=0x80;(*buffer)[size-1]=0x5a; }
        if (overflow) (*buffer)[size]=0;
        if (underflow) (*buffer)[-1]=0;
    }
    return read_status;
}
static void reset(void)
{
    memcpy(bios,original,sizeof(bios));
    for (unsigned i=0;i<24;i++) *(uintptr_t *)(bios+GLINK_PROTOCOL_RVA+8+i*8)=(uintptr_t)(bios+callback_rvas[i]);
    *(uintptr_t *)(bios+0x4238)=(uintptr_t)&boot;bios[0x43e1]=1;
    li.image_base=bios;li.image_size=GLINK_SIZE;
    image_count=protocol_count=1;reads=arms=disarms=0;bad_arm=bad_pointer=bad_buffer=0;read_status=0;
    overflow=underflow=no_copy=all_zero=0;
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
#ifdef Q1N1_USBC_BUFFER_PROBE
    reset();check(0,1,"RAW +000: 0x8000000000000200");
    reset();check(0,1,"RAW +080: 0x000000005A000000");
#else
    reset();check(0,1,"UCSI VERSION (raw): 0x0000000000000200");
    reset();check(0,1,"UCSI CCI (raw): 0x0000000080000000");
#endif
    reset();protocol_count=2;check(0,1,"snapshot complete");
    reset();image_count=0;check(EFI_UNSUPPORTED,0,"Matching BIOS312");
    reset();image_count=2;check(EFI_UNSUPPORTED,0,"Matching BIOS312");
    reset();bios[0x1000]^=1;check(EFI_UNSUPPORTED,0,"Matching BIOS312");
    reset();protocol_count=0;check(EFI_UNSUPPORTED,0,"protocol absent");
    reset();bad_pointer=1;check(EFI_UNSUPPORTED,0,"differs from inspected");
    reset();bios[GLINK_PROTOCOL_RVA]=0x0b;check(EFI_UNSUPPORTED,0,"revision mismatch");
    reset();*(uintptr_t *)(bios+GLINK_PROTOCOL_RVA+8)=1;check(EFI_UNSUPPORTED,0,"callback layout");
    reset();bios[0x43e1]=0;check(EFI_UNSUPPORTED,0,"not ready");
    reset();*(uintptr_t *)(bios+0x4238)=1;check(EFI_UNSUPPORTED,0,"not ready");
    reset();bad_arm=1;check(EFI_UNSUPPORTED,0,"Could not arm");
    reset();read_status=EFI_UNSUPPORTED;check(EFI_UNSUPPORTED,1,"Read failed");
    reset();bad_buffer=1;check(EFI_UNSUPPORTED,1,"Unexpected output buffer");
    reset();overflow=1;check(EFI_UNSUPPORTED,1,"Unexpected output buffer");
    reset();underflow=1;check(EFI_UNSUPPORTED,1,"Unexpected output buffer");
    reset();no_copy=1;check(EFI_UNSUPPORTED,1,"Output buffer was not populated");
    reset();all_zero=1;check(0,1,"snapshot complete");
    printf("PASS: %u " PROBE_NAME " read/guard cases; captured firmware never executed\n",cases);
    return 0;
}

/* SPDX-License-Identifier: MIT */
/* Host mocks: captured AArch64 firmware bytes are DATA, never executed. */
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "../platform/uefi/usb-typec-probe.c"

static uint8_t original[POWER_IMAGE_SIZE], bios[POWER_IMAGE_SIZE] __attribute__((aligned(8)));
static uint8_t table[4096] __attribute__((aligned(4096)));
static struct efi_loaded_image li;
static struct efi_boot_services boot;
static struct efi_text console;
static struct efi_system_table st;
static efi_handle image_handles[] = {(void *)1, (void *)2};
static efi_handle power_handles[] = {(void *)3, (void *)4};
static void *power_pointer;
static unsigned image_count, power_count, ram_type, cases;
static int bad_map;
static char log_data[12000];
static size_t log_used;
static int same(efi_guid *a, efi_guid *b) { return !memcmp(a,b,sizeof(*a)); }
static efi_status output(struct efi_text *unused, const char16 *s)
{
    (void)unused;
    while (*s && log_used + 1 < sizeof(log_data)) log_data[log_used++] = (char)*s++;
    log_data[log_used] = 0;
    return 0;
}
static efi_status locate(uint32_t type, efi_guid *guid, void *key, uint64_t *n, efi_handle **h)
{
    assert(type == EFI_BY_PROTOCOL && !key);
    if (same(guid,&loaded_guid)) { *n=image_count; *h=image_handles; return 0; }
    assert(same(guid,&power_guid));
    if (!power_count) return EFI_NOT_FOUND;
    *n=power_count; *h=power_handles; return 0;
}
static efi_status protocol(efi_handle h, efi_guid *guid, void **p)
{
    (void)h;
    if (same(guid,&loaded_guid)) *p=&li;
    else { assert(same(guid,&power_guid)); *p=power_pointer; }
    return 0;
}
static efi_status free_pool(void *p) { (void)p; return 0; }
static efi_status get_map(uint64_t *size, void *p, uint64_t *key, uint64_t *stride, uint32_t *version)
{
    (void)key; (void)version;
    if (bad_map) return EFI_BUFFER_TOO_SMALL;
    assert(*size >= 40);
    memset(p,0,40); *(uint32_t *)p=ram_type;
    *(uint64_t *)((uint8_t *)p+8)=(uintptr_t)table;
    *(uint64_t *)((uint8_t *)p+24)=1;
    *size=40; *stride=40; return 0;
}
static void reset(void)
{
    memcpy(bios,original,sizeof(bios)); memset(table,0,sizeof(table));
    for (unsigned i=0;i<14;i++) *(uintptr_t *)(bios+POWER_INTERFACE+8+8*i)=(uintptr_t)(bios+callbacks[i]);
    li.image_base=bios; li.image_size=POWER_IMAGE_SIZE;
    image_count=power_count=1; ram_type=4; bad_map=0;
    power_pointer=bios+POWER_INTERFACE;
    bios[PORT_COUNT_RVA]=2; bios[CACHE_READY_RVA]=1;
    *(uintptr_t *)(bios+PORT_TABLE_RVA)=(uintptr_t)table;
    table[1]=0; table[2]=1;
    *(uint32_t *)(table+0x30)=6; *(uint32_t *)(table+PORT_STRIDE+0x30)=2;
    *(uint32_t *)(bios+CACHE_RVA)=3u<<24;
    log_used=0; log_data[0]=0;
}
static void check(int success, const char *needle)
{
    efi_status result=efi_main((void *)1,&st);
    if (result != (success ? 0 : EFI_UNSUPPORTED) || !strstr(log_data,needle)) {
        fprintf(stderr,"%s\nExpected: %s\n",log_data,needle); abort();
    }
    cases++;
}
int main(int argc,char **argv)
{
    assert(argc==2); FILE *f=fopen(argv[1],"rb"); assert(f);
    assert(fread(original,1,sizeof(original),f)==sizeof(original)); fclose(f);
    assert(fingerprint(original+0x1000,0x2000)==POWER_CODE_FNV);
    boot.locate_handle_buffer=locate; boot.handle_protocol=protocol;
    boot.free_pool=free_pool; boot.get_memory_map=get_map;
    console.output=output; st.boot=&boot; st.console_out=&console;
    /* All other boot services and vendor functions are deliberately unusable. */
    reset(); check(1,"Cached power role: source");
    reset(); *(uint32_t *)(bios+CACHE_RVA)=1u<<24; check(1,"Cached power role: sink");
    reset(); *(uint32_t *)(bios+CACHE_RVA)=0; check(1,"Cached connection: disconnected");
    reset(); bios[CACHE_READY_RVA]=0; check(1,"Cache decode unavailable");
    reset(); power_count=2; check(1,"Snapshot complete");
    reset(); *(uint32_t *)(table+0x30)=2; check(1,"Cache decode unavailable");
    reset(); *(uint32_t *)(table+0x30)=2; *(uint32_t *)(table+0x34)=6; check(1,"Cached power role: source");
    reset(); image_count=0; check(0,"Matching BIOS312");
    reset(); image_count=2; check(0,"Matching BIOS312");
    reset(); bios[0x1000]^=1; check(0,"Matching BIOS312");
    reset(); power_count=0; check(0,"protocol absent");
    reset(); power_pointer=(void *)1; check(0,"not the inspected static object");
    reset(); *(uint64_t *)(bios+POWER_INTERFACE)=0x10001; check(0,"revision mismatch");
    reset(); *(uintptr_t *)(bios+POWER_INTERFACE+8)=1; check(0,"callback layout mismatch");
    reset(); bad_map=1; check(0,"RAM bounds");
    reset(); *(uintptr_t *)(bios+PORT_TABLE_RVA)=1; check(0,"port-list allocation");
    reset(); ram_type=11; check(0,"port-list allocation");
    reset(); bios[PORT_COUNT_RVA]=7; check(0,"port-list allocation");
    reset(); table[2]=0; check(0,"Port ID");
    reset(); table[2]=6; check(0,"Port ID");
    reset(); *(uintptr_t *)(bios+PORT_TABLE_RVA)=(uintptr_t)table+4088; table[4089]=5; check(0,"Port ID");
    printf("PASS: %u Type-C snapshot/guard cases; no vendor callbacks executed\n",cases);
    return 0;
}

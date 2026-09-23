/* SPDX-License-Identifier: MIT */
#include "preload.h"
#include "sha256.h"
#include <assert.h>
#include <stdlib.h>
#include <stdio.h>
#include <string.h>

static struct q1n1_preload_manifest manifest;
static uint8_t payload[8197];
static size_t manifest_bytes, payload_bytes, reported_bytes, cursor[2];
static unsigned handles_count, allocation_calls, fail_allocation, live_allocations;
static int absent, short_read, read_error, extra_byte;
static unsigned refusal_checks;
static int lazy_filesystem, storage_connected, connect_error;
static struct efi_file root, mf, pf;
static efi_status close_file(struct efi_file *f) {(void)f;return 0;}
static efi_status open_file(struct efi_file *f,struct efi_file **out,const char16 *name,uint64_t mode,uint64_t attrs)
{
    (void)f;assert(mode==1 && !attrs);
    char text[256];unsigned i=0;
    do {text[i]=(char)name[i];} while(name[i++]);
    if(!strcmp(text,Q1N1_PRELOAD_PATH)) {
        if(absent) return EFI_NOT_FOUND;
        *out=&mf;cursor[0]=0;return 0;
    }
    if(strcmp(text,"\\asset.bin")) return EFI_NOT_FOUND;
    *out=&pf;cursor[1]=0;return 0;
}
static efi_status get_info(struct efi_file *f,efi_guid *guid,uint64_t *n,void *data)
{
    (void)guid;assert(*n>=80);
    memset(data,0,80);((uint64_t *)data)[0]=80;
    ((uint64_t *)data)[1]=f==&mf?manifest_bytes:reported_bytes;
    *n=80;return 0;
}
static efi_status read_file(struct efi_file *f,uint64_t *n,void *data)
{
    unsigned idx=f==&mf?0:1;
    const void *source=idx?(void *)payload:(void *)&manifest;
    size_t bytes=idx?payload_bytes:manifest_bytes;
    if(idx && read_error) return EFI_ERROR(7);
    if(cursor[idx]==bytes) {
        if(idx && extra_byte) {*(uint8_t *)data=42;*n=1;extra_byte=0;return 0;}
        *n=0;return 0;
    }
    if(*n>bytes-cursor[idx]) *n=bytes-cursor[idx];
    if(short_read && *n>13) *n=13;
    memcpy(data,(const uint8_t *)source+cursor[idx],(size_t)*n);cursor[idx]+=(size_t)*n;
    return 0;
}
static struct efi_simple_fs fs;
static efi_status open_volume(struct efi_simple_fs *s,struct efi_file **out)
{(void)s;*out=&root;return 0;}
static efi_status locate(uint32_t type,efi_guid *guid,void *key,uint64_t *count,efi_handle **handles)
{
    (void)key;assert(type==EFI_BY_PROTOCOL);
    if(guid->a==0x964e5b22 && lazy_filesystem && !storage_connected) return EFI_NOT_FOUND;
    *count=guid->a==0x964e5b21?1:handles_count;
    *handles=calloc((size_t)*count,sizeof(**handles));return 0;
}
static efi_status connect_storage(efi_handle h,efi_handle *drivers,void *path,uint8_t recursive)
{
    (void)h;assert(!drivers && !path && recursive==1);
    if(connect_error) return EFI_UNSUPPORTED;
    storage_connected=1;return 0;
}
static efi_status protocol(efi_handle handle,efi_guid *guid,void **out)
{(void)handle;(void)guid;*out=&fs;return 0;}
static efi_status allocate(uint32_t mode,uint32_t kind,uint64_t pages,uint64_t *address)
{
    assert(mode==0 && kind==EFI_LOADER_DATA);
    if(++allocation_calls==fail_allocation) return EFI_ERROR(9);
    void *p=NULL;assert(!posix_memalign(&p,4096,(size_t)pages*4096));
    *address=(uintptr_t)p;live_allocations++;return 0;
}
static efi_status free_pages(uint64_t address,uint64_t pages)
{assert(pages && live_allocations);free((void *)(uintptr_t)address);live_allocations--;return 0;}
static efi_status free_pool(void *p) {free(p);return 0;}
static struct efi_boot_services bs={.allocate_pages=allocate,.free_pages=free_pages,
    .free_pool=free_pool,.locate_handle_buffer=locate,.handle_protocol=protocol,
    .connect_controller=connect_storage};
static void reset(void)
{
    assert(!live_allocations);
    memset(&manifest,0,sizeof(manifest));
    manifest.magic=Q1N1_PRELOAD_MAGIC;manifest.version=1;manifest.count=1;
    manifest.entry_size=sizeof(manifest.files[0]);
    manifest_bytes=32+sizeof(manifest.files[0]);
    for(unsigned i=0;i<sizeof(payload);i++) payload[i]=(uint8_t)i;
    payload_bytes=reported_bytes=sizeof(payload);
    manifest.files[0].size=payload_bytes;strcpy(manifest.files[0].path,"\\asset.bin");
    q1n1_sha256(payload,payload_bytes,manifest.files[0].sha256);
    allocation_calls=fail_allocation=0;handles_count=1;
    absent=short_read=read_error=extra_byte=0;
    lazy_filesystem=storage_connected=connect_error=0;
    mf=(struct efi_file){.close=close_file,.get_info=get_info,.read=read_file};pf=mf;
    root=(struct efi_file){.open=open_file,.close=close_file};fs.open_volume=open_volume;
}
static void refused(void)
{
    uint64_t address=123,size=456;
    assert(q1n1_preload(&bs,&address,&size));
    assert(!address && !size && !live_allocations);
    refusal_checks++;
}
static void successful(void)
{
    uint64_t address=0,size=0;assert(!q1n1_preload(&bs,&address,&size));
    struct q1n1_preload_cache *c=(void *)(uintptr_t)address;
    assert(size==sizeof(*c) && c->count==1 && live_allocations==2);
    assert(q1n1_preload_diagnostics.phase==7 && !q1n1_preload_diagnostics.status);
    assert(q1n1_preload_diagnostics.block_devices==1 && q1n1_preload_diagnostics.connect_successes==1);
    assert(q1n1_preload_diagnostics.read_bytes==payload_bytes);
    assert(q1n1_preload_verify(c,0));
    assert(!q1n1_preload_verify(c,1));
    assert(!memcmp((void *)(uintptr_t)c->files[0].base,payload,payload_bytes));
    ((uint8_t *)(uintptr_t)c->files[0].base)[4096]^=1;
    assert(!q1n1_preload_verify(c,0));
    free_pages(c->files[0].base,c->files[0].allocation/4096);
    free_pages(address,(sizeof(*c)+4095)/4096);
}
static void digest(const void *p,size_t n,const char *expected)
{
    uint8_t hash[32];char text[65];q1n1_sha256(p,n,hash);
    for(unsigned i=0;i<32;i++) snprintf(text+2*i,3,"%02x",hash[i]);
    assert(!strcmp(text,expected));
    struct q1n1_sha256 s;q1n1_sha256_init(&s);
    for(size_t i=0;i<n;i++) q1n1_sha256_update(&s,(const uint8_t *)p+i,1);
    q1n1_sha256_final(&s,hash);
    for(unsigned i=0;i<32;i++) snprintf(text+2*i,3,"%02x",hash[i]);
    assert(!strcmp(text,expected));
}
int main(void)
{
    digest("",0,"e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855");
    digest("abc",3,"ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
    const char *long_input="abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq";
    digest(long_input,strlen(long_input),"248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1");
    char *million=malloc(1000000);memset(million,'a',1000000);
    digest(million,1000000,"cdc76e5c9914fb9281a1c7e284d73e67f1809a48a497200e046d39ccc7112cd0");free(million);
    reset();successful();reset();short_read=1;successful();
    reset();lazy_filesystem=1;successful();
    reset();lazy_filesystem=1;connect_error=1;refused();
    assert(q1n1_preload_diagnostics.phase==2 && !q1n1_preload_diagnostics.filesystems);
    reset();strcpy(manifest.files[0].path,"\\missing.bin");refused();
    assert(q1n1_preload_diagnostics.phase==4 && q1n1_preload_diagnostics.manifests==1);
    reset();absent=1;refused();
    assert(q1n1_preload_diagnostics.phase==2 && !q1n1_preload_diagnostics.manifests);
    reset();handles_count=2;refused();
    reset();manifest.magic=0;refused();reset();manifest.version=2;refused();
    reset();manifest.count=17;refused();reset();manifest.count=0;refused();
    reset();manifest.entry_size++;refused();reset();manifest_bytes--;refused();
    reset();manifest_bytes=8;refused();reset();manifest.files[0].size=0;refused();
    reset();manifest.files[0].size=Q1N1_PRELOAD_LIMIT+1;refused();
    reset();strcpy(manifest.files[0].path,"\\..\\asset.bin");refused();
    reset();strcpy(manifest.files[0].path,"\\x\\\\asset.bin");refused();
    reset();memset(manifest.files[0].path,'x',256);refused();
    reset();manifest.count=2;manifest.files[1]=manifest.files[0];manifest_bytes+=296;refused();
    reset();manifest.count=2;manifest.files[1]=manifest.files[0];manifest_bytes+=296;
    strcpy(manifest.files[1].path,"\\ASSET.bin");refused();
    reset();reported_bytes--;refused();reset();payload_bytes--;refused();
    reset();read_error=1;refused();reset();extra_byte=1;refused();
    reset();payload[100]^=1;refused();reset();fail_allocation=1;refused();
    reset();fail_allocation=2;refused();
    printf("PASS: SHA256 vectors/streaming, EFI reads/reservations, %u refusal paths, cleanup, and resident corruption\n",refusal_checks);
    return 0;
}

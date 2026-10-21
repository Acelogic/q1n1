/* SPDX-License-Identifier: MIT */
#include "preload.h"
#include "sha256.h"
static int equal(const void *a,const void *b,size_t n)
{
    const uint8_t *x=a,*y=b;
    for(size_t i=0;i<n;i++) if(x[i]!=y[i]) return 0;
    return 1;
}
static int valid_path(const char *p)
{
    if(p[0]!='\\') return 0;
    unsigned start=1;
    for(unsigned i=1;i<256;i++) {
        unsigned char c=(unsigned char)p[i];
        if(!c || c=='\\') {
            if(i==start || (i-start==1 && p[start]=='.') ||
               (i-start==2 && p[start]=='.' && p[start+1]=='.')) return 0;
            if(!c) {
                for(unsigned j=i+1;j<256;j++) if(p[j]) return 0;
                return 1;
            }
            start=i+1;
        } else if(c<32 || c>126 || c==':' || c=='/' || c=='*' || c=='?' || c=='"' || c=='<' || c=='>' || c=='|') return 0;
    }
    return 0;
}
static int same_path(const char *a,const char *b)
{
    for(unsigned i=0;i<256;i++) {
        unsigned char x=(unsigned char)a[i],y=(unsigned char)b[i];
        if(x>='A' && x<='Z') x+='a'-'A';
        if(y>='A' && y<='Z') y+='a'-'A';
        if(x!=y) return 0;
    }
    return 1;
}
int q1n1_preload_manifest_valid(const struct q1n1_preload_manifest *m,uint64_t size)
{
    if(size<32 || m->magic!=Q1N1_PRELOAD_MAGIC || m->version!=1 ||
       !m->count || m->count>Q1N1_PRELOAD_MAX || m->entry_size!=sizeof(m->files[0]) ||
       size!=32+m->count*sizeof(m->files[0])) return 0;
    uint64_t total=0;
    for(unsigned i=0;i<m->count;i++) {
        const struct q1n1_preload_file *f=&m->files[i];
        if(!f->size || f->size>Q1N1_PRELOAD_LIMIT-total || !valid_path(f->path)) return 0;
        total+=f->size;
        for(unsigned j=0;j<i;j++) if(same_path(f->path,m->files[j].path)) return 0;
    }
    return 1;
}
int q1n1_preload_verify(const struct q1n1_preload_cache *c,uint64_t index)
{
    if(!c || c->magic!=Q1N1_CACHE_MAGIC || c->version!=1 || c->count>Q1N1_PRELOAD_MAX ||
       c->entry_size!=sizeof(c->files[0]) || index>=c->count) return 0;
    const struct q1n1_cached_file *f=&c->files[index];
    if(!f->base || (f->base&4095) || !f->file.size || f->file.size>Q1N1_PRELOAD_LIMIT ||
       f->allocation!=((f->file.size+4095)&~UINT64_C(4095)) || f->base>UINT64_MAX-f->allocation) return 0;
    uint8_t digest[32];
    q1n1_sha256((const void *)(uintptr_t)f->base,(size_t)f->file.size,digest);
    return equal(digest,f->file.sha256,32);
}
#ifndef Q1N1_STAGE
struct q1n1_preload_diagnostics q1n1_preload_diagnostics;
static efi_guid block_guid={0x964e5b21,0x6459,0x11d2,{0x8e,0x39,0,0xa0,0xc9,0x69,0x72,0x3b}};
static efi_guid fs_guid={0x964e5b22,0x6459,0x11d2,{0x8e,0x39,0,0xa0,0xc9,0x69,0x72,0x3b}};
static efi_guid info_guid={0x09576e92,0x6d3f,0x11d2,{0x8e,0x39,0,0xa0,0xc9,0x69,0x72,0x3b}};
static void wide(char16 *dst,const char *src) { do {*dst++=(uint8_t)*src;} while(*src++); }
static efi_status file_size(struct efi_file *file,uint64_t *size)
{
    /* EFI_FILE_INFO: size, file size, physical size, 3 EFI_TIMEs, attributes,
     * then a variable name. A 256-character manifest path bounds our names. */
    uint64_t data[128],n=sizeof(data);
    efi_status s=file->get_info(file,&info_guid,&n,data);
    if(s) return s;
    if(n<80 || n>sizeof(data) || data[0]<80 || data[0]>n || (data[9]&0x10)) return EFI_INVALID_PARAMETER;
    *size=data[1];return 0;
}
static efi_status read_exact(struct efi_file *file,void *data,uint64_t size)
{
    uint8_t *p=data;
    while(size) {
        uint64_t n=size>1048576?1048576:size,want=n;
        efi_status s=file->read(file,&n,p);
        if(s) return s;
        if(!n || n>want) return EFI_ERROR(7);
        p+=n;size-=n;
        q1n1_preload_diagnostics.read_bytes+=n;
    }
    uint8_t extra;uint64_t n=1;
    efi_status s=file->read(file,&n,&extra);
    return s?s:n?EFI_INVALID_PARAMETER:0;
}
static void release(struct efi_boot_services *bs,struct q1n1_preload_cache *c,uint64_t address)
{
    for(unsigned i=0;i<Q1N1_PRELOAD_MAX;i++)
        if(c->files[i].base) bs->free_pages(c->files[i].base,c->files[i].allocation/4096);
    bs->free_pages(address,(sizeof(*c)+4095)/4096);
}
efi_status q1n1_preload(struct efi_boot_services *bs,uint64_t *address,uint64_t *size)
{
    *address=0;*size=0;
    struct q1n1_preload_diagnostics *d=&q1n1_preload_diagnostics;
    *d=(struct q1n1_preload_diagnostics){.version=1,.size=sizeof(*d),.phase=1};
    /* A firmware boot path may connect only its ESP. Ask existing storage
     * handles to connect their partition/filesystem children. Do not disconnect
     * controllers or touch raw media; unrelated USB controllers are not scanned. */
    if(bs->connect_controller) {
        efi_handle *blocks=NULL;uint64_t block_count=0;
        if(!bs->locate_handle_buffer(EFI_BY_PROTOCOL,&block_guid,NULL,&block_count,&blocks)) {
            d->block_devices=block_count;
            for(uint64_t i=0;i<block_count;i++)
                if(!bs->connect_controller(blocks[i],NULL,NULL,1)) d->connect_successes++;
            bs->free_pool(blocks);
        }
    }
    d->phase=2;
    efi_handle *handles=NULL;uint64_t count=0;
    efi_status status=bs->locate_handle_buffer(EFI_BY_PROTOCOL,&fs_guid,NULL,&count,&handles);
    if(status) {d->status=status;return status;}
    d->filesystems=count;
    struct efi_file *chosen=NULL,*manifest=NULL;
    char16 name[256];wide(name,Q1N1_PRELOAD_PATH);
    status=EFI_NOT_FOUND;
    for(uint64_t i=0;i<count;i++) {
        struct efi_simple_fs *fs=NULL;struct efi_file *root=NULL,*file=NULL;
        if(bs->handle_protocol(handles[i],&fs_guid,(void **)&fs) || !fs || fs->open_volume(fs,&root)) continue;
        d->roots++;
        efi_status opened=root->open(root,&file,name,1,0);
        if(opened) {root->close(root);continue;}
        d->manifests++;
        if(chosen) {file->close(file);root->close(root);status=EFI_INVALID_PARAMETER;goto done;}
        chosen=root;manifest=file;status=0;
    }
    if(!chosen) goto done;
    d->phase=3;
    /* EFI entry is single-threaded. Keep this bounded parser buffer out of
     * the stack (the freestanding COFF target has no __chkstk runtime). */
    static struct q1n1_preload_manifest m;
    uint64_t bytes=0;
    status=file_size(manifest,&bytes);
    if(status) goto done;
    if(bytes>sizeof(m)) {status=EFI_INVALID_PARAMETER;goto done;}
    status=read_exact(manifest,&m,bytes);
    if(status) goto done;
    if(!q1n1_preload_manifest_valid(&m,bytes)) {status=EFI_INVALID_PARAMETER;goto done;}
    uint64_t table=0;
    status=bs->allocate_pages(0,EFI_LOADER_DATA,(sizeof(struct q1n1_preload_cache)+4095)/4096,&table);
    if(status) goto done;
    struct q1n1_preload_cache *c=(void *)(uintptr_t)table;
    for(size_t i=0;i<sizeof(*c);i++) ((uint8_t *)c)[i]=0;
    c->magic=Q1N1_CACHE_MAGIC;c->version=1;c->count=m.count;c->entry_size=sizeof(c->files[0]);
    for(unsigned i=0;i<m.count;i++) {
        struct q1n1_cached_file *entry=&c->files[i];struct efi_file *file=NULL;
        entry->file=m.files[i];wide(name,entry->file.path);
        d->phase=4;d->asset_index=i;d->expected_bytes=entry->file.size;d->read_bytes=0;
        status=chosen->open(chosen,&file,name,1,0);
        if(status) break;
        status=file_size(file,&bytes);
        if(!status && bytes!=entry->file.size) status=EFI_INVALID_PARAMETER;
        if(!status) {
            entry->allocation=(bytes+4095)&~UINT64_C(4095);
            status=bs->allocate_pages(0,EFI_LOADER_DATA,entry->allocation/4096,&entry->base);
            if(!status) {
                d->phase=5;
                status=read_exact(file,(void *)(uintptr_t)entry->base,bytes);
                if(!status) {
                    d->phase=6;
                    if(!q1n1_preload_verify(c,i)) status=EFI_ERROR(27);
                }
            } else entry->base=0;
        }
        file->close(file);
        if(status) break;
    }
    if(status) release(bs,c,table);
    else {*address=table;*size=sizeof(*c);d->phase=7;}
done:
    d->status=status;
    if(manifest) manifest->close(manifest);
    if(chosen) chosen->close(chosen);
    bs->free_pool(handles);
    return status;
}
#endif

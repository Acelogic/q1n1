# SPDX-License-Identifier: MIT
# Applied only to the reference-capture Mesa copy in the Linux VM (never shipped): lets the GL
# driver load air2ir3 blobs instead of compiling, so Mesa's own a8xx program state is emitted for
# the exact ir3 code the A16 tool uploads.
p = "src/freedreno/ir3/ir3_shader.c"
s = open(p).read()
hook = r"""
#include <stdio.h>
#include "util/blob.h"
void p67_retrieve_variant(struct blob_reader *blob, struct ir3_shader_variant *v);
static bool
p67_override(struct ir3_shader_variant *v)
{
   const char *path = v->type == MESA_SHADER_VERTEX ? getenv("P67_VARIANT_VS") :
                      v->type == MESA_SHADER_FRAGMENT ? getenv("P67_VARIANT_FS") : NULL;
   if (!path)
      return false;
   FILE *f = fopen(path, "rb");
   if (!f) { fprintf(stderr, "p67: cannot open %s\n", path); abort(); }
   fseek(f, 0, SEEK_END); long n = ftell(f); fseek(f, 0, SEEK_SET);
   void *buf = malloc(n);
   if (fread(buf, 1, n, f) != (size_t)n) abort();
   fclose(f);
   struct blob_reader blob;
   blob_reader_init(&blob, buf, n);
   struct ir3_shader_key key;
   blob_copy_bytes(&blob, &key, sizeof(key));
   uint32_t type = blob_read_uint32(&blob);
   uint32_t merged = blob_read_uint32(&blob);
   if (type != v->type) { fprintf(stderr, "p67: %s is stage %u, not %u\n", path, type, v->type); abort(); }
   v->mergedregs = merged;
   p67_retrieve_variant(&blob, v);
   if (v->binning)
      p67_retrieve_variant(&blob, v->binning);
   if (blob.overrun || blob.current != blob.end) {
      fprintf(stderr, "p67: %s did not parse exactly (overrun %d, %ld left)\n", path, blob.overrun,
              (long)(blob.end - blob.current));
      abort();
   }
   fprintf(stderr, "p67: variant %s loaded (%u bytes of code)\n", path, v->info.size);
   free(buf);
   return true;
}
"""
anchor = "static struct ir3_shader_variant *\ncreate_variant("
assert anchor in s
s = s.replace(anchor, hook + "\n" + anchor, 1)
old = "   if (ir3_disk_cache_retrieve(shader, v))\n      return v;"
assert old in s
s = s.replace(old, "   if (p67_override(v))\n      return v;\n\n" + old, 1)
open(p, "w").write(s)
q = "src/freedreno/ir3/ir3_disk_cache.c"
t = open(q).read()
old = "static void\nretrieve_variant(struct blob_reader *blob, struct ir3_shader_variant *v)\n{"
assert old in t
t = t.replace(old, "static void retrieve_variant(struct blob_reader *blob, struct ir3_shader_variant *v);\n"
              "void p67_retrieve_variant(struct blob_reader *blob, struct ir3_shader_variant *v);\n"
              "void\np67_retrieve_variant(struct blob_reader *blob, struct ir3_shader_variant *v)\n{\n"
              "   retrieve_variant(blob, v);\n}\n\n" + old, 1)
open(q, "w").write(t)
print("patched")

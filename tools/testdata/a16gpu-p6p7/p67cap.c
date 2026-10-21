/* SPDX-License-Identifier: MIT */
/* p67cap: drive Mesa's freedreno gallium driver (a8xx, drm-shim noop kernel) through the same
 * three operations as a16gpu P6/P7 so FD_RD_DUMP captures Mesa's own command streams:
 *   1. clear of a 256x256 RGBA8 render target (sysmem fast clear -> 2D engine)
 *   2. glBlitFramebuffer 256x256 src -> dst (2D engine)
 *   3. one textured triangle: VS mvp from a std140 UBO, FS texture(s, v), linear clamp_to_edge
 * Each is flushed separately so each lands in its own submit. */
#define EGL_EGLEXT_PROTOTYPES
#include <EGL/egl.h>
#include <EGL/eglext.h>
#include <GLES3/gl3.h>
#ifndef GL_BGRA_EXT
#define GL_BGRA_EXT 0x80E1
#endif
#include <GLES2/gl2ext.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define CK(x) do { x; GLenum e = glGetError(); if (e) { fprintf(stderr, "%s: GL error 0x%x\n", #x, e); exit(1);} } while (0)

static GLuint shader(GLenum kind, const char *src) {
  GLuint s = glCreateShader(kind); glShaderSource(s, 1, &src, NULL); glCompileShader(s);
  GLint ok; glGetShaderiv(s, GL_COMPILE_STATUS, &ok);
  if (!ok) { char log[2048]; glGetShaderInfoLog(s, sizeof log, NULL, log); fprintf(stderr, "%s\n", log); exit(1); }
  return s;
}

int main(int argc, char **argv) {
  int W = 256, H = 256;
  const char *mode = argc > 1 ? argv[1] : "all";
  int bgra = argc > 2 && !strcmp(argv[2], "bgra");
  EGLDisplay d = eglGetPlatformDisplay(EGL_PLATFORM_SURFACELESS_MESA, EGL_DEFAULT_DISPLAY, NULL);
  if (!eglInitialize(d, NULL, NULL)) { fprintf(stderr, "eglInitialize\n"); return 1; }
  eglBindAPI(EGL_OPENGL_ES_API);
  EGLint cattr[] = { EGL_RENDERABLE_TYPE, EGL_OPENGL_ES3_BIT, EGL_NONE }, n;
  EGLConfig cfg; eglChooseConfig(d, cattr, &cfg, 1, &n);
  EGLint ctxattr[] = { EGL_CONTEXT_MAJOR_VERSION, 3, EGL_NONE };
  EGLContext c = eglCreateContext(d, n ? cfg : EGL_NO_CONFIG_KHR, EGL_NO_CONTEXT, ctxattr);
  if (!c || !eglMakeCurrent(d, EGL_NO_SURFACE, EGL_NO_SURFACE, c)) { fprintf(stderr, "context\n"); return 1; }
  fprintf(stderr, "GL_RENDERER %s\n", glGetString(GL_RENDERER));

  unsigned char *px = malloc(W * H * 4);
  for (int y = 0; y < H; y++) for (int x = 0; x < W; x++) {
    unsigned char *p = px + 4 * (y * W + x); p[0] = x; p[1] = y; p[2] = x ^ y; p[3] = 255; }
  GLuint tex[3], fbo[2];
  glGenTextures(3, tex); glGenFramebuffers(2, fbo);
  for (int i = 0; i < 2; i++) {
    glBindTexture(GL_TEXTURE_2D, tex[i]);
    if (bgra) { CK(glTexImage2D(GL_TEXTURE_2D, 0, GL_BGRA_EXT, W, H, 0, GL_BGRA_EXT, GL_UNSIGNED_BYTE, NULL)); }
    else { CK(glTexStorage2D(GL_TEXTURE_2D, 1, GL_RGBA8, W, H)); }
    glBindFramebuffer(GL_FRAMEBUFFER, fbo[i]);
    CK(glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, tex[i], 0));
  }
  glBindTexture(GL_TEXTURE_2D, tex[0]);
  CK(glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, W, H, bgra ? GL_BGRA_EXT : GL_RGBA, GL_UNSIGNED_BYTE, px));
  glFinish();
  if (getenv("P67_MARK")) fprintf(stderr, "== setup done\n");
  if (!strcmp(mode, "cleartex")) {
    /* EXT_clear_texture: gallium clear_texture -> fd6_clear_texture (a nondraw 2D clear of a sub-rectangle) */
    const char *ext = (const char *)glGetString(GL_EXTENSIONS);
    fprintf(stderr, "EXT_clear_texture %s\n", strstr(ext, "GL_EXT_clear_texture") ? "yes" : "no");
    PFNGLCLEARTEXSUBIMAGEEXTPROC ctsi = (PFNGLCLEARTEXSUBIMAGEEXTPROC)eglGetProcAddress("glClearTexSubImageEXT");
    unsigned char color[4] = { 0x00, 0x40, 0xff, 0xff };   /* BGRA bytes of RGBA (1.0, 0.25, 0.0, 1.0) */
    fprintf(stderr, "== cleartex\n");
    CK(ctsi(tex[1], 0, 16, 8, 0, 208, 224, 1, bgra ? GL_BGRA_EXT : GL_RGBA, GL_UNSIGNED_BYTE, color));
    glFinish();
  }
  if (!strcmp(mode, "clear") || !strcmp(mode, "all")) {
  fprintf(stderr, "== clear\n");
  glBindFramebuffer(GL_FRAMEBUFFER, fbo[1]); glViewport(0, 0, W, H);
  glClearColor(0.1f, 0.2f, 0.3f, 1.0f); CK(glClear(GL_COLOR_BUFFER_BIT));
  glFinish(); }
  if (!strcmp(mode, "blit") || !strcmp(mode, "all")) {
  fprintf(stderr, "== blit\n");
  glBindFramebuffer(GL_READ_FRAMEBUFFER, fbo[0]); glBindFramebuffer(GL_DRAW_FRAMEBUFFER, fbo[1]);
  CK(glBlitFramebuffer(0, 0, W, H, 0, 0, W, H, GL_COLOR_BUFFER_BIT, GL_NEAREST));
  glFinish(); }
  if (!strcmp(mode, "tri") || !strcmp(mode, "all")) {
  fprintf(stderr, "== triangle\n");
  /* 64x64 texture for the triangle, as P7 */
  glBindTexture(GL_TEXTURE_2D, tex[2]);
  CK(glTexStorage2D(GL_TEXTURE_2D, 1, GL_RGBA8, 64, 64));
  CK(glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, 64, 64, GL_RGBA, GL_UNSIGNED_BYTE, px));
  glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
  glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
  glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
  glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
  glFinish();
  const char *vs = "#version 300 es\nlayout(std140) uniform U { mat4 mvp; };\n"
    "layout(location=0) in vec2 pos; layout(location=1) in vec2 tc; out vec2 v;\n"
    "void main() { gl_Position = mvp * vec4(pos, 0.0, 1.0); v = tc; }\n";
  const char *fs = "#version 300 es\nprecision highp float; uniform sampler2D s; in vec2 v; out vec4 o;\n"
    "void main() { o = texture(s, v); }\n";
  GLuint prog = glCreateProgram();
  glAttachShader(prog, shader(GL_VERTEX_SHADER, vs)); glAttachShader(prog, shader(GL_FRAGMENT_SHADER, fs));
  glLinkProgram(prog); CK(glUseProgram(prog));
  float mvp[16] = { 2.0f/96, 0, 0, 0,  0, -2.0f/64, 0, 0,  0, 0, 1, 0,  -1, 1, 0, 1 };
  GLuint ubo; glGenBuffers(1, &ubo); glBindBuffer(GL_UNIFORM_BUFFER, ubo);
  CK(glBufferData(GL_UNIFORM_BUFFER, sizeof mvp, mvp, GL_STATIC_DRAW));
  CK(glUniformBlockBinding(prog, glGetUniformBlockIndex(prog, "U"), 1));
  CK(glBindBufferBase(GL_UNIFORM_BUFFER, 1, ubo));
  float vtx[] = { 8, 4, 0, 0,  88, 4, 1, 0,  8, 60, 0, 1 };
  GLuint vbo; glGenBuffers(1, &vbo); glBindBuffer(GL_ARRAY_BUFFER, vbo);
  CK(glBufferData(GL_ARRAY_BUFFER, sizeof vtx, vtx, GL_STATIC_DRAW));
  glVertexAttribPointer(0, 2, GL_FLOAT, GL_FALSE, 16, (void *)0); glEnableVertexAttribArray(0);
  glVertexAttribPointer(1, 2, GL_FLOAT, GL_FALSE, 16, (void *)8); glEnableVertexAttribArray(1);
  glActiveTexture(GL_TEXTURE0); glBindTexture(GL_TEXTURE_2D, tex[2]);
  glUniform1i(glGetUniformLocation(prog, "s"), 0);
  glBindFramebuffer(GL_FRAMEBUFFER, fbo[1]); glViewport(0, 0, 96, 64);
  glDisable(GL_DEPTH_TEST); glDisable(GL_BLEND); glDisable(GL_CULL_FACE);
  glClearColor(0.1f, 0.2f, 0.3f, 1.0f); CK(glClear(GL_COLOR_BUFFER_BIT));
  CK(glDrawArrays(GL_TRIANGLES, 0, 3));
  glFinish(); }
  fprintf(stderr, "== done\n");
  return 0;
}

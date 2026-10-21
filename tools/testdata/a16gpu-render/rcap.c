/* SPDX-License-Identifier: MIT */
/* rcap: drive Mesa's freedreno gallium driver (a8xx, drm-shim noop kernel) through the state classes `render`
 * adds to P7, so FD_RD_DUMP captures Mesa's own command streams for them (tools/test-a16gpu-render.py
 * test_mesa_state). One mode per run, one flushed draw each, 96x64 targets like the render scenes:
 *   mrt     two colour attachments R8 + RG8 (ColorFillYCbCr's targets), FS writes both
 *   blend   BGRA8 target, premultiplied source-over (ONE, ONE_MINUS_SRC_ALPHA)
 *   fbfetch BGRA8 target, EXT_shader_framebuffer_fetch (the sysmem fb-read descriptor in bindless set 4)
 *   tex     RGBA16F 64x64 (nearest, repeat) and RGBA8 128x96 (linear, clamp) textures sampled by the FS
 *   tex1d   desktop GL: RGBA8 1D 256 and 1D-array 256x6 textures (linear, clamp)
 *   vid     no vertex attributes, gl_VertexID (the vertex-id sideband)
 * Build: cc -O1 rcap.c -o rcap -lEGL -lGLESv2 (Mesa's libs from the capture build; desktop-only entry points
 * through eglGetProcAddress). */
#define EGL_EGLEXT_PROTOTYPES
#include <EGL/egl.h>
#include <EGL/eglext.h>
#include <GLES3/gl32.h>
#include <GLES2/gl2ext.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#ifndef GL_TEXTURE_1D
#define GL_TEXTURE_1D 0x0DE0
#endif
#ifndef GL_TEXTURE_1D_ARRAY
#define GL_TEXTURE_1D_ARRAY 0x8C18
#endif
typedef void (*teximage1d_fn)(GLenum, GLint, GLint, GLsizei, GLint, GLenum, GLenum, const void *);

#define CK(x) do { x; GLenum e = glGetError(); if (e) { fprintf(stderr, "%s: GL error 0x%x\n", #x, e); exit(1);} } while (0)
#define W 96
#define H 64

static GLuint shader(GLenum kind, const char *src) {
  GLuint s = glCreateShader(kind); glShaderSource(s, 1, &src, NULL); glCompileShader(s);
  GLint ok; glGetShaderiv(s, GL_COMPILE_STATUS, &ok);
  if (!ok) { char log[4096]; glGetShaderInfoLog(s, sizeof log, NULL, log); fprintf(stderr, "%s\n%s\n", src, log); exit(1); }
  return s;
}

static GLuint program(const char *vs, const char *fs) {
  GLuint p = glCreateProgram();
  glAttachShader(p, shader(GL_VERTEX_SHADER, vs)); glAttachShader(p, shader(GL_FRAGMENT_SHADER, fs));
  glLinkProgram(p);
  GLint ok; glGetProgramiv(p, GL_LINK_STATUS, &ok);
  if (!ok) { char log[4096]; glGetProgramInfoLog(p, sizeof log, NULL, log); fprintf(stderr, "%s\n", log); exit(1); }
  return p;
}

static void context(int desktop) {
  EGLDisplay d = eglGetPlatformDisplay(EGL_PLATFORM_SURFACELESS_MESA, EGL_DEFAULT_DISPLAY, NULL);
  if (!eglInitialize(d, NULL, NULL)) { fprintf(stderr, "eglInitialize\n"); exit(1); }
  eglBindAPI(desktop ? EGL_OPENGL_API : EGL_OPENGL_ES_API);
  EGLint ctxattr[] = { EGL_CONTEXT_MAJOR_VERSION, desktop ? 3 : 3, EGL_CONTEXT_MINOR_VERSION, desktop ? 3 : 2,
                       desktop ? EGL_CONTEXT_OPENGL_PROFILE_MASK : EGL_NONE, EGL_CONTEXT_OPENGL_CORE_PROFILE_BIT, EGL_NONE };
  EGLContext c = eglCreateContext(d, EGL_NO_CONFIG_KHR, EGL_NO_CONTEXT, ctxattr);
  if (!c || !eglMakeCurrent(d, EGL_NO_SURFACE, EGL_NO_SURFACE, c)) { fprintf(stderr, "context\n"); exit(1); }
  fprintf(stderr, "GL_RENDERER %s / %s\n", glGetString(GL_RENDERER), glGetString(GL_VERSION));
}

static GLuint target(GLenum internal, GLenum format, GLenum type) {
  GLuint t; glGenTextures(1, &t); glBindTexture(GL_TEXTURE_2D, t);
  CK(glTexImage2D(GL_TEXTURE_2D, 0, internal, W, H, 0, format, type, NULL));
  glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
  return t;
}

static void quad(GLuint prog) {
  static const float v[] = { -0.8f, -0.8f, 0.8f, -0.8f, -0.8f, 0.8f, 0.8f, -0.8f, 0.8f, 0.8f, -0.8f, 0.8f };
  GLuint vao, vbo; glGenVertexArrays(1, &vao); glBindVertexArray(vao);
  glGenBuffers(1, &vbo); glBindBuffer(GL_ARRAY_BUFFER, vbo); glBufferData(GL_ARRAY_BUFFER, sizeof v, v, GL_STATIC_DRAW);
  GLint loc = glGetAttribLocation(prog, "pos");
  if (loc >= 0) { glEnableVertexAttribArray(loc); glVertexAttribPointer(loc, 2, GL_FLOAT, GL_FALSE, 8, 0); }
  glUseProgram(prog); glViewport(0, 0, W, H);
  CK(glDrawArrays(GL_TRIANGLES, 0, 6));
  glFinish();
}

static const char *VS_ES = "#version 320 es\nin vec2 pos; out vec2 uv;\n"
                           "void main() { uv = pos * 0.5 + 0.5; gl_Position = vec4(pos, 0.0, 1.0); }\n";
static const char *VS_GL = "#version 330 core\nin vec2 pos; out vec2 uv;\n"
                           "void main() { uv = pos * 0.5 + 0.5; gl_Position = vec4(pos, 0.0, 1.0); }\n";

int main(int argc, char **argv) {
  const char *mode = argc > 1 ? argv[1] : "";
  context(!strcmp(mode, "tex1d"));
  GLuint fbo; glGenFramebuffers(1, &fbo); glBindFramebuffer(GL_FRAMEBUFFER, fbo);
  if (!strcmp(mode, "mrt")) {
    GLuint t0 = target(GL_R8, GL_RED, GL_UNSIGNED_BYTE), t1 = target(GL_RG8, GL_RG, GL_UNSIGNED_BYTE);
    CK(glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, t0, 0));
    CK(glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT1, GL_TEXTURE_2D, t1, 0));
    GLenum bufs[2] = { GL_COLOR_ATTACHMENT0, GL_COLOR_ATTACHMENT1 }; glDrawBuffers(2, bufs);
    quad(program(VS_ES, "#version 320 es\nprecision highp float; in vec2 uv;\n"
                        "layout(location = 0) out float y; layout(location = 1) out vec2 cbcr;\n"
                        "void main() { y = uv.x; cbcr = uv.yx; }\n"));
  } else if (!strcmp(mode, "blend") || !strcmp(mode, "fbfetch")) {
    GLuint t0 = target(GL_BGRA_EXT, GL_BGRA_EXT, GL_UNSIGNED_BYTE);
    CK(glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, t0, 0));
    if (!strcmp(mode, "blend")) {
      glEnable(GL_BLEND); glBlendFunc(GL_ONE, GL_ONE_MINUS_SRC_ALPHA); glBlendEquation(GL_FUNC_ADD);
      quad(program(VS_ES, "#version 320 es\nprecision highp float; in vec2 uv; out vec4 c;\n"
                          "void main() { c = vec4(uv * 0.5, 0.2, 0.5); }\n"));
    } else {
      quad(program(VS_ES, "#version 320 es\n#extension GL_EXT_shader_framebuffer_fetch : require\n"
                          "precision highp float; in vec2 uv; inout vec4 c;\n"
                          "void main() { c = c * (1.0 - 0.45) + vec4(0.2, 0.1, 0.35, 0.45); }\n"));
    }
  } else if (!strcmp(mode, "tex")) {
    GLuint t0 = target(GL_BGRA_EXT, GL_BGRA_EXT, GL_UNSIGNED_BYTE);
    CK(glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, t0, 0));
    GLuint tex[2]; glGenTextures(2, tex);
    glActiveTexture(GL_TEXTURE0); glBindTexture(GL_TEXTURE_2D, tex[0]);
    CK(glTexStorage2D(GL_TEXTURE_2D, 1, GL_RGBA16F, 64, 64));
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST); glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_REPEAT); glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_REPEAT);
    glActiveTexture(GL_TEXTURE1); glBindTexture(GL_TEXTURE_2D, tex[1]);
    CK(glTexStorage2D(GL_TEXTURE_2D, 1, GL_RGBA8, 128, 96));
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR); glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE); glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
    GLuint p = program(VS_ES, "#version 320 es\nprecision highp float; in vec2 uv; out vec4 c;\n"
                              "uniform highp sampler2D a; uniform highp sampler2D b;\n"
                              "void main() { c = texture(a, uv) * texture(b, uv); }\n");
    glUseProgram(p); glUniform1i(glGetUniformLocation(p, "a"), 0); glUniform1i(glGetUniformLocation(p, "b"), 1);
    quad(p);
  } else if (!strcmp(mode, "tex1d")) {
    GLuint t0 = target(GL_RGBA8, GL_RGBA, GL_UNSIGNED_BYTE);
    CK(glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, t0, 0));
    GLuint tex[2]; glGenTextures(2, tex);
    glActiveTexture(GL_TEXTURE0); glBindTexture(GL_TEXTURE_1D, tex[0]);
    teximage1d_fn glTexImage1D = (teximage1d_fn)eglGetProcAddress("glTexImage1D");
    CK(glTexImage1D(GL_TEXTURE_1D, 0, GL_RGBA8, 256, 0, GL_RGBA, GL_UNSIGNED_BYTE, NULL));
    glTexParameteri(GL_TEXTURE_1D, GL_TEXTURE_MIN_FILTER, GL_LINEAR); glTexParameteri(GL_TEXTURE_1D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_1D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
    glActiveTexture(GL_TEXTURE1); glBindTexture(GL_TEXTURE_1D_ARRAY, tex[1]);
    CK(glTexImage2D(GL_TEXTURE_1D_ARRAY, 0, GL_RGBA8, 256, 6, 0, GL_RGBA, GL_UNSIGNED_BYTE, NULL));
    glTexParameteri(GL_TEXTURE_1D_ARRAY, GL_TEXTURE_MIN_FILTER, GL_LINEAR); glTexParameteri(GL_TEXTURE_1D_ARRAY, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_1D_ARRAY, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
    GLuint p = program(VS_GL, "#version 330 core\nin vec2 uv; out vec4 c;\n"
                              "uniform sampler1D a; uniform sampler1DArray b;\n"
                              "void main() { c = texture(a, uv.x) * texture(b, vec2(uv.y, 3.0)); }\n");
    glUseProgram(p); glUniform1i(glGetUniformLocation(p, "a"), 0); glUniform1i(glGetUniformLocation(p, "b"), 1);
    quad(p);
  } else if (!strcmp(mode, "vid")) {
    GLuint t0 = target(GL_BGRA_EXT, GL_BGRA_EXT, GL_UNSIGNED_BYTE);
    CK(glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, t0, 0));
    quad(program("#version 320 es\nout vec2 uv;\n"
                 "void main() { vec2 p = vec2(float(gl_VertexID & 1), float(gl_VertexID >> 1)) * 1.6 - 0.8;\n"
                 "  uv = p; gl_Position = vec4(p, 0.0, 1.0); }\n",
                 "#version 320 es\nprecision highp float; in vec2 uv; out vec4 c;\nvoid main() { c = vec4(uv, 0.5, 1.0); }\n"));
  } else {
    fprintf(stderr, "usage: rcap mrt|blend|fbfetch|tex|tex1d|vid\n");
    return 2;
  }
  return 0;
}

/* SPDX-License-Identifier: MIT */
#include "boot-window.h"
#include <stdio.h>
#include <stdlib.h>

static void check(int condition, const char *case_name)
{
    if (!condition) { fprintf(stderr, "FAIL: %s\n", case_name); exit(1); }
}

int main(void)
{
    /* USB0 can enumerate even if the host never runs a16ctl. That case used
     * to send unattended q1n1 resets to Windows. */
    check(q1n1_timeout_choice(1, 1) == Q1N1_BOOT_PROXY,
          "auto with configured USB0 stays in q1n1");
    check(q1n1_timeout_choice(1, 0) == Q1N1_BOOT_PROXY_NCM,
          "auto without USB0 discovers direct USB");
    check(q1n1_timeout_choice(0, 1) == Q1N1_BOOT_WINDOWS,
          "manual with USB0 retains Windows timeout");
    check(q1n1_timeout_choice(0, 0) == Q1N1_BOOT_WINDOWS,
          "manual without USB0 retains Windows timeout");
    puts("PASS: 4 boot timeout choices");
}

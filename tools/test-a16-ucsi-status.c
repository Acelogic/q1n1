/* SPDX-License-Identifier: MIT */
/* Reuse the independently tested driver-identity fixture, then exercise command state transitions. */
#define main register_fixture_main
#define efi_main ucsi_register_preflight
#include "test-a16-ucsi-reg.c"
#undef main
#undef efi_main
#define Q1N1_UCSI_REG_EMBEDDED
#define Q1N1_UCSI_STATUS_HOST_TEST
#include "../platform/uefi/usb-ucsi-status.c"
static uint64_t commands[32],active;
static unsigned writes,property_reads,stalls,scenario;
enum {SUCCESS,INITIAL_BUSY,STALE_CCI,ERROR_CCI,WRITE_FAIL,READ_FAIL,BAD_SIZE,BAD_COUNT,
      GUARD,NO_COPY,REMOTE_ERROR,LINK_DROP,ACK_STALE,ONE_PORT,STALE_MESSAGE,UNSTABLE_MESSAGE,FIRST_STALE};
static unsigned message_samples;
efi_status mock_control_write(uint64_t cmd)
{
    assert(query_allowed(cmd) && writes<32);commands[writes++]=cmd;active=cmd;message_samples=0;
    return scenario==WRITE_FAIL ? EFI_ERROR(7) : 0;
}
efi_status mock_property_read(uint32_t reg,uint8_t *data,uint32_t size)
{
    property_reads++;
    if (scenario==READ_FAIL) return EFI_ERROR(7);
    if (scenario==NO_COPY) return 0;
    memset(data,0,size);
    if (reg==0x20104) {
        assert(size==4);uint32_t cci=0;
        if (!writes) cci=scenario==INITIAL_BUSY ? CCI_BUSY : 0;
        else if (scenario==STALE_CCI) cci=0;
        else if (scenario==ERROR_CCI) cci=CCI_COMPLETE|(1u<<30);
        else if (active==0x20004) cci=scenario==ACK_STALE ? CCI_COMPLETE : CCI_ACK;
        else {
            unsigned n=(active&255)==6 ? 16 : (active&255)==7 ? 4 : (active&255)==0x12 ? 19 : 0;
            if (scenario==BAD_SIZE && (active&255)==6) n=33;
            cci=CCI_COMPLETE|(n<<8);
        }
        memcpy(data,&cci,4);
    } else {
        assert(reg==0x20200);
#ifdef UCSI_STATUS_FULL_BLOCK
        assert(size==64);
#else
        assert(size<=32);
#endif
        unsigned command=0;for (unsigned n=writes;n;n--) if (commands[n-1]!=0x20004) {command=(unsigned)commands[n-1];break;}
        if ((command&255)==6) data[4]=scenario==BAD_COUNT ? 3 : scenario==ONE_PORT ? 1 : 2;
        else if ((command&255)==7) {data[0]=3;data[1]=0xc;}
        else {assert((command&255)==0x12);uint32_t value=(2u<<29)|(1u<<19)|(3u<<16);memcpy(data,&value,4);}
        message_samples++;
        if (scenario==STALE_MESSAGE || (scenario==FIRST_STALE && message_samples==1)) {
            uint32_t cci=CCI_COMPLETE|(((command&255)==6 ? 16 : (command&255)==7 ? 4 : 19)<<8);memcpy(data,&cci,4);
        }
        if (scenario==UNSTABLE_MESSAGE) data[0]=(uint8_t)message_samples;
    }
    if (scenario==GUARD) data[size]=0;
    if (scenario==REMOTE_ERROR) bios[0x4950]=1;
    if (scenario==LINK_DROP) bios[0x43e1]=0;
    return 0;
}
static efi_status mock_stall(uint64_t usec) {assert(usec==20000);stalls++;return 0;}
static void command_case(unsigned which,efi_status expected,unsigned expected_writes)
{
    reset();writes=property_reads=stalls=0;active=0;scenario=which;boot.stall=(void *)mock_stall;
    efi_status status=efi_main((void *)1,&st);
    if (status!=expected || writes!=expected_writes) {
        fprintf(stderr,"scenario %u status %llx writes %u\n%s",which,(unsigned long long)status,writes,log_data);abort();
    }
    assert(arms==disarms && property_reads<=90);
    if (which==SUCCESS) {
        const uint64_t expected_commands[]={0x10005,0x20004,6,0x20004,0x10007,0x20004,0x10012,0x20004,0x20007,0x20004,0x20012,0x20004,5};
        assert(writes==sizeof(expected_commands)/sizeof(expected_commands[0]));
        assert(!memcmp(commands,expected_commands,sizeof(expected_commands)));
        assert(strstr(log_data,"Partner is UFP/device; local data role is host."));
    }
    cases++;
}
int main(int argc,char **argv)
{
    register_fixture_main(argc,argv);
    command_case(SUCCESS,0,13);
    command_case(ONE_PORT,0,9);
    command_case(INITIAL_BUSY,EFI_UNSUPPORTED,0);
    command_case(STALE_CCI,EFI_ERROR(18),1);
    command_case(ERROR_CCI,EFI_ERROR(7),1);
    command_case(WRITE_FAIL,EFI_ERROR(7),1);
    command_case(READ_FAIL,EFI_ERROR(7),0);
    command_case(BAD_SIZE,EFI_UNSUPPORTED,4);
    command_case(BAD_COUNT,EFI_UNSUPPORTED,4);
    command_case(GUARD,EFI_UNSUPPORTED,0);
    command_case(NO_COPY,EFI_UNSUPPORTED,0);
    command_case(REMOTE_ERROR,EFI_ERROR(7),0);
    command_case(LINK_DROP,EFI_UNSUPPORTED,0);
    command_case(ACK_STALE,EFI_ERROR(18),2);
#ifdef UCSI_STATUS_FULL_BLOCK
    command_case(STALE_MESSAGE,EFI_UNSUPPORTED,4);
    command_case(UNSTABLE_MESSAGE,EFI_UNSUPPORTED,4);
    command_case(FIRST_STALE,0,13);
#endif
    unsigned before=writes;
    assert(control_write(bios,1)==EFI_UNSUPPORTED); /* PPM_RESET */
    assert(control_write(bios,0x1010009)==EFI_UNSUPPORTED); /* SET_UOR */
    assert(control_write(bios,0x1000000010005ULL)==EFI_UNSUPPORTED); /* reserved bit */
    assert(control_write(bios,0x30012)==EFI_UNSUPPORTED); /* unknown connector */
    assert(property_read(bios,0x20108,bios,8)==EFI_UNSUPPORTED);
    assert(property_read(bios,0x20200,bios,33)==EFI_UNSUPPORTED);
    assert(writes==before);cases++;
    printf("PASS: %u combined fixture/state/allowlist cases; firmware never executed\n",cases);
    return 0;
}

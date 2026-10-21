/* SPDX-License-Identifier: MIT */
#define main register_fixture_main
#define efi_main ucsi_register_preflight
#include "test-a16-ucsi-reg.c"
#undef main
#undef efi_main
#define Q1N1_UCSI_REG_EMBEDDED
#define Q1N1_UCSI_STATUS_HOST_TEST
#include "../platform/uefi/usb-ucsi-device0.c"
static uint64_t active;
static unsigned writes,role_requests,properties,error_queries,scenario;
enum {SUCCESS,REJECT_ROLE,TIMEOUT_ROLE,WRONG_AFTER,NO_USB,NO_PD,SOURCE,NO_UFP,WRONG_COUNT,WRITE_ERROR,NO_OPT,BAD_OPT,DUP_OPT,STALE_MESSAGE,ZERO_ROLE_ERROR,ALREADY_DEVICE,ALREADY_DEVICE_NO_USB,ALREADY_DEVICE_SOURCE};
efi_status mock_control_write(uint64_t command)
{
    assert(query_allowed(command));active=command;writes++;
    if((command&255)==0x13) {assert(command==0x10013);error_queries++;}
    if(command==0x3010009) {role_requests++;assert(role_requests==1);if(scenario==WRITE_ERROR)return EFI_ERROR(7);}
    return 0;
}
efi_status mock_property_read(uint32_t reg,uint8_t *data,uint32_t size)
{
    properties++;memset(data,0,size);
    if(reg==0x20104) {
        assert(size==4);uint32_t cci=0;
        if(writes) {
            unsigned len=(active&255)==6?16:(active&255)==7?4:(active&255)==0x12?19:(active&255)==0x13?16:0;
            cci=active==0x20004?CCI_ACK:CCI_COMPLETE|(len<<8);
            if(active==0x3010009 && (scenario==REJECT_ROLE || scenario==ZERO_ROLE_ERROR))cci|=1u<<30;
            if(active==0x3010009 && scenario==TIMEOUT_ROLE)cci=CCI_BUSY;
        }
        memcpy(data,&cci,4);
    } else {
        assert(reg==0x20200 && size==64);
        if((active&255)==6) {data[0]=4;data[4]=scenario==WRONG_COUNT?4:2;}
        else if((active&255)==7) {data[0]=scenario==NO_UFP?5:7;data[1]=4;} /* incoming UFP swaps disabled */
        else if((active&255)==0x13) {
            assert(active==0x10013); /* UCSI 2.1: error belongs to connector 1. */
            if(scenario!=ZERO_ROLE_ERROR) data[1]=8; /* PPM policy conflict */
        }
        else {
            assert((active&255)==0x12);
            uint32_t value=(role_requests && scenario!=WRONG_AFTER?1u:2u)<<29;
            if(scenario==ALREADY_DEVICE || scenario==ALREADY_DEVICE_NO_USB || scenario==ALREADY_DEVICE_SOURCE)
                value=1u<<29;
            value|=(1u<<19)|(1u<<21)|(3u<<16);
            if(scenario==NO_USB || scenario==ALREADY_DEVICE_NO_USB)value&=~(1u<<21);
            if(scenario==NO_PD)value&=~(7u<<16);
            if(scenario==SOURCE || scenario==ALREADY_DEVICE_SOURCE)value|=1u<<20;
            memcpy(data,&value,4);
        }
        if(scenario==STALE_MESSAGE) {uint32_t cci=CCI_COMPLETE|(16u<<8);memcpy(data,&cci,4);}
    }
    return 0;
}
static efi_status stall(uint64_t us) {assert(us==20000);return 0;}
static char16 options[]={ 'a','p','p',' ','-','-','d','e','v','i','c','e','0',0 };
static char16 bad[]={ '-','-','d','e','v','i','c','e','1',0 };
static char16 dup[]={ '-','-','d','e','v','i','c','e','0',' ','-','-','d','e','v','i','c','e','0',0 };
static void role_case(unsigned which,efi_status expected,unsigned requests)
{
    reset();scenario=which;writes=role_requests=properties=error_queries=0;active=0;boot.stall=(void *)stall;
    li.options=options;li.options_size=sizeof(options);
    if(which==NO_OPT)li.options=NULL;
    if(which==BAD_OPT) {li.options=bad;li.options_size=sizeof(bad);}
    if(which==DUP_OPT) {li.options=dup;li.options_size=sizeof(dup);}
    efi_status status=efi_main((void *)1,&st);
    if(status!=expected || role_requests!=requests) {
        fprintf(stderr,"scenario %u status %llx role requests %u\n%s",which,(unsigned long long)status,role_requests,log_data);abort();
    }
    assert(arms==disarms && properties<100);
    if(which==SUCCESS || which==ALREADY_DEVICE)assert(strstr(log_data,"A16 UFP/device and partner DFP/host confirmed."));
    if(which==ALREADY_DEVICE)assert(strstr(log_data,"already reports the desired data roles; no role request."));
    if(which==REJECT_ROLE || which==ZERO_ROLE_ERROR) {
        assert(error_queries==1 && strstr(log_data,"Connector 1 error flags:"));
        assert(strstr(log_data,"Serial must not start."));
        if(which==ZERO_ROLE_ERROR) assert(strstr(log_data,"rejection reason remains unresolved"));
    } else assert(error_queries==0);
    cases++;
}
int main(int argc,char **argv)
{
    register_fixture_main(argc,argv);
    role_case(SUCCESS,0,1);
    role_case(ALREADY_DEVICE,0,0);
    role_case(ALREADY_DEVICE_NO_USB,EFI_UNSUPPORTED,0);
    role_case(ALREADY_DEVICE_SOURCE,EFI_UNSUPPORTED,0);
    role_case(REJECT_ROLE,EFI_ERROR(7),1);
    role_case(ZERO_ROLE_ERROR,EFI_ERROR(7),1);
    role_case(TIMEOUT_ROLE,EFI_ERROR(18),1);
    role_case(WRITE_ERROR,EFI_ERROR(7),1);
    role_case(WRONG_AFTER,EFI_UNSUPPORTED,1);
    role_case(NO_USB,EFI_UNSUPPORTED,0);
    role_case(NO_PD,EFI_UNSUPPORTED,0);
    role_case(SOURCE,EFI_UNSUPPORTED,0);
    role_case(NO_UFP,EFI_UNSUPPORTED,0);
    role_case(WRONG_COUNT,EFI_UNSUPPORTED,0);
    role_case(NO_OPT,EFI_UNSUPPORTED,0);
    role_case(BAD_OPT,EFI_UNSUPPORTED,0);
    role_case(DUP_OPT,EFI_UNSUPPORTED,0);
    role_case(STALE_MESSAGE,EFI_UNSUPPORTED,0);
    assert(!query_allowed(0x3020009));assert(!query_allowed(0x301000b));assert(!query_allowed(0x1010009));assert(!query_allowed(1));
    assert(!query_allowed(0x13));assert(!query_allowed(0x20013));
    printf("PASS: %u firmware-guard/role-state cases; firmware never executed\n",cases);
    return 0;
}

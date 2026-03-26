/*
 * cfr_enable.ko v6 - Full CFR unlock with init intercept
 * 
 * Patches TWO functions:
 * 1. wlan_cfr_is_feature_disabled -> returns 0 (not disabled)
 * 2. init_deinit_cfr_support_enable -> forces CFR support on
 *
 * After loading, run "wifi down && wifi up" to reinit with CFR enabled.
 */

#include <linux/module.h>
#include <linux/kernel.h>
#include <linux/kallsyms.h>

MODULE_LICENSE("GPL");
MODULE_AUTHOR("Spectral Project");
MODULE_DESCRIPTION("Full CFR unlock for UniFi APs v6");

struct patch_info {
    unsigned long addr;
    unsigned int original[2];
    int patched;
    const char *name;
};

static struct patch_info patches[2] = {
    { .name = "wlan_cfr_is_feature_disabled" },
    { .name = "init_deinit_cfr_support_enable" },
};
static int num_patches = 0;

/* ARM: mov r0, #0; bx lr */
static const unsigned int ret_zero[2] = { 0xE3A00000, 0xE12FFF1E };

/* ARM: mov r0, #1; bx lr (for init_deinit - force enable) */
/* Actually init_deinit_cfr_support_enable doesn't return a value - 
 * it reads the service bit and calls target_if_cfr_set_cfr_support().
 * We need to replace it with a version that always calls set_cfr_support(psoc, 1).
 * 
 * Simplest approach: make it a no-op (return immediately) and instead
 * patch wmi_service_enabled to return true for the CFR service check.
 *
 * OR: patch init_deinit_cfr_support_enable to just do:
 *   push {lr}
 *   mov r1, #1           @ is_cfr_support = 1  
 *   bl target_if_cfr_set_cfr_support
 *   pop {pc}
 *
 * But we don't know the relative offset for bl. 
 * Simpler: just make it always call set_cfr_support with arg=1.
 * The function takes (psoc) and we need to call set_cfr_support(psoc, 1).
 * r0 already has psoc when this function is called.
 * So: mov r1, #1; b target_if_cfr_set_cfr_support
 */

static int (*fn_set_memory_rw)(unsigned long addr, int numpages);
static int (*fn_set_memory_ro)(unsigned long addr, int numpages);

static void do_cache_flush(unsigned long addr)
{
    asm volatile(
        "mcr p15, 0, %0, c7, c11, 1\n"
        "dsb\n"
        "mcr p15, 0, %0, c7, c5, 1\n"
        "dsb\n"
        "isb\n"
        :: "r"(addr)
    );
}

static int apply_patch(struct patch_info *p, const unsigned int *insn)
{
    unsigned int *code = (unsigned int *)p->addr;
    unsigned long page = p->addr & PAGE_MASK;
    
    p->original[0] = code[0];
    p->original[1] = code[1];
    
    pr_info("cfr_enable: %s @ 0x%lx: 0x%08x 0x%08x -> 0x%08x 0x%08x\n",
            p->name, p->addr, p->original[0], p->original[1], insn[0], insn[1]);
    
    fn_set_memory_rw(page, 1);
    code[0] = insn[0];
    code[1] = insn[1];
    do_cache_flush(p->addr);
    do_cache_flush(p->addr + 4);
    fn_set_memory_ro(page, 1);
    
    p->patched = (code[0] == insn[0] && code[1] == insn[1]);
    return p->patched ? 0 : -EIO;
}

static void restore_patch(struct patch_info *p)
{
    if (p->patched && p->addr) {
        unsigned int *code = (unsigned int *)p->addr;
        unsigned long page = p->addr & PAGE_MASK;
        fn_set_memory_rw(page, 1);
        code[0] = p->original[0];
        code[1] = p->original[1];
        do_cache_flush(p->addr);
        do_cache_flush(p->addr + 4);
        fn_set_memory_ro(page, 1);
        pr_info("cfr_enable: restored %s\n", p->name);
    }
}

static int __init cfr_enable_init(void)
{
    unsigned long set_cfr_support_addr;
    
    fn_set_memory_rw = (void *)kallsyms_lookup_name("set_memory_rw");
    fn_set_memory_ro = (void *)kallsyms_lookup_name("set_memory_ro");
    if (!fn_set_memory_rw || !fn_set_memory_ro) {
        pr_err("cfr_enable: set_memory_rw/ro not found\n");
        return -ENOENT;
    }

    /* Patch 1: wlan_cfr_is_feature_disabled -> return 0 */
    patches[0].addr = kallsyms_lookup_name("wlan_cfr_is_feature_disabled");
    if (!patches[0].addr) {
        pr_err("cfr_enable: wlan_cfr_is_feature_disabled not found\n");
        return -ENOENT;
    }
    if (apply_patch(&patches[0], ret_zero)) {
        pr_err("cfr_enable: patch 1 failed\n");
        return -EIO;
    }
    num_patches = 1;

    /* Patch 2: init_deinit_cfr_support_enable -> force call set_cfr_support(psoc, 1) */
    patches[1].addr = kallsyms_lookup_name("init_deinit_cfr_support_enable");
    set_cfr_support_addr = kallsyms_lookup_name("target_if_cfr_set_cfr_support");
    
    if (patches[1].addr && set_cfr_support_addr) {
        /* Build: mov r1, #1; b target_if_cfr_set_cfr_support
         * The branch offset = (target - (pc+8)) / 4
         * pc = patches[1].addr + 4 (second instruction)
         * ARM branch: 0xEA000000 | (offset & 0x00FFFFFF)
         */
        long offset = ((long)set_cfr_support_addr - (long)(patches[1].addr + 4 + 8)) / 4;
        unsigned int branch_insn = 0xEA000000 | (offset & 0x00FFFFFF);
        unsigned int init_patch[2] = { 0xE3A01001, branch_insn }; /* mov r1, #1; b target */
        
        pr_info("cfr_enable: set_cfr_support @ 0x%lx, branch offset %ld (0x%08x)\n",
                set_cfr_support_addr, offset, branch_insn);
        
        if (apply_patch(&patches[1], init_patch)) {
            pr_err("cfr_enable: patch 2 failed\n");
        } else {
            num_patches = 2;
        }
    } else {
        pr_warn("cfr_enable: init_deinit_cfr_support_enable or target not found\n");
    }

    pr_info("cfr_enable: %d patches applied\n", num_patches);
    pr_info("cfr_enable: NOW RUN: wifi down && sleep 3 && wifi up\n");
    pr_info("cfr_enable: Then:    cfg80211tool wifi1 cfr_timer 1\n");
    pr_info("cfr_enable:          cfr_test_app -i wifi1\n");
    
    return 0;
}

static void __exit cfr_enable_exit(void)
{
    int i;
    for (i = num_patches - 1; i >= 0; i--)
        restore_patch(&patches[i]);
    pr_info("cfr_enable: all patches restored\n");
}

module_init(cfr_enable_init);
module_exit(cfr_enable_exit);

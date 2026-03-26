/*
 * cfr_enable.ko v5 - Full CFR unlock for UniFi APs
 * 
 * 1. Patches wlan_cfr_is_feature_disabled() to return 0
 * 2. Calls tgt_cfr_support_set() to mark CFR as supported  
 * 3. Calls cfr_initialize_pdev() to init the CFR subsystem
 */

#include <linux/module.h>
#include <linux/kernel.h>
#include <linux/kallsyms.h>

MODULE_LICENSE("GPL");
MODULE_AUTHOR("Spectral Project");
MODULE_DESCRIPTION("Full CFR unlock for UniFi APs");

static unsigned long func_addr = 0;
static unsigned int original_insn[2] = {0, 0};
static int patched = 0;

static const unsigned int ret_zero[2] = { 0xE3A00000, 0xE12FFF1E };

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

static void patch_function(unsigned long addr)
{
    unsigned int *p = (unsigned int *)addr;
    unsigned long page = addr & PAGE_MASK;
    
    fn_set_memory_rw(page, 1);
    p[0] = ret_zero[0];
    p[1] = ret_zero[1];
    do_cache_flush(addr);
    do_cache_flush(addr + 4);
    fn_set_memory_ro(page, 1);
}

static int __init cfr_enable_init(void)
{
    unsigned int *p;
    
    /* Resolve memory functions */
    fn_set_memory_rw = (void *)kallsyms_lookup_name("set_memory_rw");
    fn_set_memory_ro = (void *)kallsyms_lookup_name("set_memory_ro");
    if (!fn_set_memory_rw || !fn_set_memory_ro) {
        pr_err("cfr_enable: set_memory_rw/ro not found\n");
        return -ENOENT;
    }

    /* Patch wlan_cfr_is_feature_disabled */
    func_addr = kallsyms_lookup_name("wlan_cfr_is_feature_disabled");
    if (!func_addr) {
        pr_err("cfr_enable: target not found\n");
        return -ENOENT;
    }
    
    p = (unsigned int *)func_addr;
    original_insn[0] = p[0];
    original_insn[1] = p[1];
    pr_info("cfr_enable: patching %lx (was 0x%08x 0x%08x)\n", 
            func_addr, original_insn[0], original_insn[1]);
    
    patch_function(func_addr);
    patched = 1;
    pr_info("cfr_enable: wlan_cfr_is_feature_disabled patched\n");

    /* Now try to initialize CFR for each pdev (radio) */
    {
        typedef void* (*get_pdev_fn)(void *psoc, uint8_t id, unsigned int dbg_id);
        typedef int (*cfr_init_fn)(void *pdev);
        typedef void (*cfr_support_fn)(void *psoc, uint8_t is_cfr_support);
        
        get_pdev_fn get_pdev = (void *)kallsyms_lookup_name("wlan_objmgr_get_pdev_by_id");
        cfr_init_fn cfr_init = (void *)kallsyms_lookup_name("cfr_initialize_pdev");
        cfr_support_fn support_set = (void *)kallsyms_lookup_name("tgt_cfr_support_set");
        unsigned long pdev_open = kallsyms_lookup_name("wlan_cfr_pdev_open");
        
        pr_info("cfr_enable: get_pdev=%px cfr_init=%px support_set=%px pdev_open=%px\n",
                get_pdev, cfr_init, support_set, pdev_open);

        /* We need a psoc pointer. Try to find it. */
        {
            /* wlan_objmgr_get_psoc_by_id(0) should give us the global psoc */
            typedef void* (*get_psoc_fn)(uint8_t id);
            get_psoc_fn get_psoc = (void *)kallsyms_lookup_name("wlan_objmgr_get_psoc_by_id");
            
            if (get_psoc) {
                void *psoc;
                int i;
                
                /* Try psoc IDs 0, 1, 2 (one per SoC/radio chip) */
                for (i = 0; i < 3; i++) {
                    psoc = get_psoc(i);
                    if (!psoc) {
                        pr_info("cfr_enable: psoc[%d] = NULL\n", i);
                        continue;
                    }
                    pr_info("cfr_enable: psoc[%d] = %px\n", i, psoc);
                    
                    /* Mark CFR as supported */
                    if (support_set) {
                        support_set(psoc, 1);
                        pr_info("cfr_enable: CFR support set for psoc[%d]\n", i);
                    }
                    
                    /* Get pdev and init CFR */
                    if (get_pdev && cfr_init) {
                        /* dbg_id 0 = WLAN_OSIF_ID */
                        void *pdev = get_pdev(psoc, 0, 0);
                        if (pdev) {
                            int ret = cfr_init(pdev);
                            pr_info("cfr_enable: cfr_initialize_pdev(psoc[%d]) = %d\n", i, ret);
                        } else {
                            pr_info("cfr_enable: no pdev for psoc[%d]\n", i);
                        }
                    }
                }
            } else {
                pr_warn("cfr_enable: get_psoc not found - CFR init skipped\n");
                pr_warn("cfr_enable: try: ifconfig wifi1ap3 down && ifconfig wifi1ap3 up\n");
            }
        }
    }

    pr_info("cfr_enable: done. Try: cfg80211tool wifi1 cfr_timer 1\n");
    return 0;
}

static void __exit cfr_enable_exit(void)
{
    if (patched && func_addr) {
        unsigned int *p = (unsigned int *)func_addr;
        unsigned long page = func_addr & PAGE_MASK;
        fn_set_memory_rw(page, 1);
        p[0] = original_insn[0];
        p[1] = original_insn[1];
        do_cache_flush(func_addr);
        do_cache_flush(func_addr + 4);
        fn_set_memory_ro(page, 1);
        pr_info("cfr_enable: restored\n");
    }
}

module_init(cfr_enable_init);
module_exit(cfr_enable_exit);

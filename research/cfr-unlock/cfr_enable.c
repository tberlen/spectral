/*
 * cfr_enable.ko - Patches wlan_cfr_is_feature_disabled to return 0
 * 
 * This enables CFR (Channel Frequency Response) capture on UniFi APs
 * where the firmware feature bit is not set.
 *
 * Load: insmod cfr_enable.ko
 * Unload: rmmod cfr_enable (restores original function)
 */

#include <linux/module.h>
#include <linux/kernel.h>
#include <linux/kallsyms.h>
#include <asm/cacheflush.h>
#include <linux/set_memory.h>

MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("Enable CFR on Ubiquiti APs");

static unsigned long func_addr = 0;
static unsigned int original_insn[2] = {0, 0};
static int patched = 0;

/*
 * ARM instructions:
 *   mov r0, #0  = 0xE3A00000
 *   bx lr       = 0xE12FFF1E
 */
static unsigned int patch_insn[2] = { 0xE3A00000, 0xE12FFF1E };

static int __init cfr_enable_init(void)
{
    unsigned int *p;
    
    func_addr = kallsyms_lookup_name("wlan_cfr_is_feature_disabled");
    if (!func_addr) {
        pr_err("cfr_enable: wlan_cfr_is_feature_disabled not found\n");
        return -ENOENT;
    }
    
    pr_info("cfr_enable: found at 0x%lx\n", func_addr);
    
    p = (unsigned int *)func_addr;
    
    /* Save original */
    original_insn[0] = p[0];
    original_insn[1] = p[1];
    pr_info("cfr_enable: original: 0x%08x 0x%08x\n", original_insn[0], original_insn[1]);
    
    /* Make page writable */
    set_memory_rw(func_addr & PAGE_MASK, 1);
    
    /* Patch */
    p[0] = patch_insn[0];
    p[1] = patch_insn[1];
    
    /* Flush icache */
    flush_icache_range(func_addr, func_addr + 8);
    
    /* Verify */
    pr_info("cfr_enable: patched: 0x%08x 0x%08x\n", p[0], p[1]);
    
    if (p[0] == patch_insn[0] && p[1] == patch_insn[1]) {
        patched = 1;
        pr_info("cfr_enable: SUCCESS - CFR feature check bypassed\n");
    } else {
        pr_err("cfr_enable: FAILED - verify mismatch\n");
        return -EIO;
    }
    
    return 0;
}

static void __exit cfr_enable_exit(void)
{
    if (patched && func_addr) {
        unsigned int *p = (unsigned int *)func_addr;
        
        set_memory_rw(func_addr & PAGE_MASK, 1);
        
        p[0] = original_insn[0];
        p[1] = original_insn[1];
        
        flush_icache_range(func_addr, func_addr + 8);
        
        pr_info("cfr_enable: restored original function\n");
    }
}

module_init(cfr_enable_init);
module_exit(cfr_enable_exit);

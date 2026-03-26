/*
 * cfr_enable.ko v7 - CFR unlock via service bitmap injection
 *
 * Strategy: Patch the host's service bitmap handler to inject CFR support
 * BEFORE the WMI_INIT_CMD is sent. The firmware then allocates DBR rings
 * for CFR because the host requested it.
 *
 * Patches:
 * 1. wlan_cfr_is_feature_disabled -> return 0
 * 2. init_deinit_cfr_support_enable -> force-enable CFR support
 * 3. wmi_service_enabled -> return 1 for CFR service check
 *    (or hook save_service_bitmap to inject bit 142)
 *
 * LOAD THIS MODULE, THEN: wifi down && sleep 3 && wifi up
 */

#include <linux/module.h>
#include <linux/kernel.h>
#include <linux/kallsyms.h>

MODULE_LICENSE("GPL");
MODULE_AUTHOR("Spectral Project");
MODULE_DESCRIPTION("CFR unlock via bitmap injection v7");

#define MAX_PATCHES 4

struct patch_info {
    unsigned long addr;
    unsigned int original[4];
    int size; /* bytes patched: 8 or 16 */
    int patched;
    const char *name;
};

static struct patch_info patches[MAX_PATCHES];
static int num_patches = 0;

static int (*fn_set_memory_rw)(unsigned long addr, int numpages);
static int (*fn_set_memory_ro)(unsigned long addr, int numpages);

static void cache_flush(unsigned long addr, int size)
{
    unsigned long a;
    for (a = addr; a < addr + size; a += 4) {
        asm volatile(
            "mcr p15, 0, %0, c7, c11, 1\n"
            "mcr p15, 0, %0, c7, c5, 1\n"
            :: "r"(a)
        );
    }
    asm volatile("dsb\nisb\n");
}

static int apply_patch(int idx, const char *name, unsigned long addr, 
                       const unsigned int *insn, int num_words)
{
    unsigned int *code;
    unsigned long page;
    int i;
    
    if (idx >= MAX_PATCHES || !addr) return -1;
    
    patches[idx].addr = addr;
    patches[idx].name = name;
    patches[idx].size = num_words * 4;
    
    code = (unsigned int *)addr;
    page = addr & PAGE_MASK;
    
    for (i = 0; i < num_words; i++)
        patches[idx].original[i] = code[i];
    
    fn_set_memory_rw(page, 1);
    /* If patch spans pages, make next page writable too */
    if (((addr + num_words*4 - 1) & PAGE_MASK) != page)
        fn_set_memory_rw((addr + num_words*4 - 1) & PAGE_MASK, 1);
    
    for (i = 0; i < num_words; i++)
        code[i] = insn[i];
    
    cache_flush(addr, num_words * 4);
    fn_set_memory_ro(page, 1);
    
    patches[idx].patched = 1;
    for (i = 0; i < num_words; i++) {
        if (code[i] != insn[i]) {
            patches[idx].patched = 0;
            break;
        }
    }
    
    pr_info("cfr_enable: [%d] %s @ 0x%lx: %s\n", idx, name, addr,
            patches[idx].patched ? "OK" : "FAILED");
    if (idx >= num_patches) num_patches = idx + 1;
    return patches[idx].patched ? 0 : -EIO;
}

static void restore_patch(int idx)
{
    unsigned int *code;
    unsigned long page;
    int i;
    
    if (!patches[idx].patched) return;
    
    code = (unsigned int *)patches[idx].addr;
    page = patches[idx].addr & PAGE_MASK;
    
    fn_set_memory_rw(page, 1);
    for (i = 0; i < patches[idx].size / 4; i++)
        code[i] = patches[idx].original[i];
    cache_flush(patches[idx].addr, patches[idx].size);
    fn_set_memory_ro(page, 1);
    
    pr_info("cfr_enable: restored %s\n", patches[idx].name);
}

static int __init cfr_enable_init(void)
{
    unsigned long addr;
    unsigned long target;
    long offset;
    
    /* ARM: mov r0, #0; bx lr */
    static const unsigned int ret0[2] = { 0xE3A00000, 0xE12FFF1E };
    /* ARM: mov r0, #1; bx lr */
    static const unsigned int ret1[2] = { 0xE3A00001, 0xE12FFF1E };
    
    fn_set_memory_rw = (void *)kallsyms_lookup_name("set_memory_rw");
    fn_set_memory_ro = (void *)kallsyms_lookup_name("set_memory_ro");
    if (!fn_set_memory_rw || !fn_set_memory_ro) {
        pr_err("cfr_enable: memory functions not found\n");
        return -ENOENT;
    }

    /* Patch 0: wlan_cfr_is_feature_disabled -> return 0 */
    addr = kallsyms_lookup_name("wlan_cfr_is_feature_disabled");
    if (addr) apply_patch(0, "cfr_is_feature_disabled", addr, ret0, 2);
    
    /* Patch 1: init_deinit_cfr_support_enable -> jump to set_cfr_support(psoc, 1) */
    addr = kallsyms_lookup_name("init_deinit_cfr_support_enable");
    target = kallsyms_lookup_name("target_if_cfr_set_cfr_support");
    if (addr && target) {
        offset = ((long)target - (long)(addr + 4 + 8)) / 4;
        unsigned int p1[2] = { 0xE3A01001, 0xEA000000 | (offset & 0x00FFFFFF) };
        apply_patch(1, "init_deinit_cfr_support_enable", addr, p1, 2);
    }
    
    /* Patch 2: Make wmi_service_enabled return TRUE when checking CFR (bit 142)
     * This is tricky - we can't return 1 for ALL services.
     * Instead, patch init_deinit_populate_service_bitmap to also set bit 142.
     * 
     * init_deinit_populate_service_bitmap is called after firmware sends service_ready.
     * After it saves the real bitmap, we want it to also set bit 142.
     * 
     * Approach: Hook the end of the function to add our bit.
     * But without seeing the code, this is risky.
     *
     * Simpler: Just patch wmi_service_enabled to check if the service ID 
     * is 142 and return 1 in that case. Otherwise call original.
     * 
     * But that requires more than 2 instructions. Let's use a trampoline.
     */
    
    /* Actually even simpler: we already patch init_deinit_cfr_support_enable
     * to force-enable CFR support. The question is whether the DBR ring
     * allocation also checks wmi_service_enabled for CFR.
     *
     * Looking at the code flow:
     * init_deinit_populate_dbr_ring_cap reads DBR caps from firmware
     * If firmware doesn't advertise CFR DBR cap, no ring is allocated
     * 
     * We might need to ALSO patch init_deinit_populate_dbr_ring_cap
     * to add a fake CFR ring entry.
     *
     * OR: the firmware MIGHT still allocate a CFR ring if CFR support
     * is implied by the INIT command. Let's try first and see.
     */
    
    pr_info("cfr_enable: %d patches applied\n", num_patches);
    pr_info("cfr_enable: NOW RUN: wifi down && sleep 3 && wifi up\n");
    
    return 0;
}

static void __exit cfr_enable_exit(void)
{
    int i;
    for (i = num_patches - 1; i >= 0; i--)
        restore_patch(i);
}

module_init(cfr_enable_init);
module_exit(cfr_enable_exit);

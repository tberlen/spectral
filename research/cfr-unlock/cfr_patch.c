#include <stdio.h>
#include <stdlib.h>
#include <fcntl.h>
#include <unistd.h>
#include <string.h>
#include <errno.h>
#include <sys/mman.h>
#include <stdint.h>

#define PHYS_OFFSET  0x41000000UL
#define PAGE_OFFSET  0x80000000UL
#define VIRT_TO_PHYS_LINEAR(v) ((v) - PAGE_OFFSET + PHYS_OFFSET)

static int memfd;

static uint32_t read_phys32(unsigned long pa) {
    void *map = mmap(NULL, 4096, PROT_READ, MAP_SHARED, memfd, pa & ~0xFFF);
    if (map == MAP_FAILED) return 0;
    uint32_t val = *(uint32_t*)((char*)map + (pa & 0xFFF));
    munmap(map, 4096);
    return val;
}

static int write_phys32(unsigned long pa, uint32_t val) {
    void *map = mmap(NULL, 4096, PROT_READ|PROT_WRITE, MAP_SHARED, memfd, pa & ~0xFFF);
    if (map == MAP_FAILED) { perror("mmap write"); return -1; }
    *(uint32_t*)((char*)map + (pa & 0xFFF)) = val;
    munmap(map, 4096);
    return 0;
}

/* Find init_mm.pgd physical address */
static unsigned long find_pgd_phys(void) {
    FILE *f = fopen("/proc/kallsyms", "r");
    if (!f) return 0;
    char line[256];
    unsigned long init_mm_addr = 0;
    while (fgets(line, sizeof(line), f)) {
        unsigned long a; char t; char n[128];
        if (sscanf(line, "%lx %c %127s", &a, &t, n) == 3) {
            if (strcmp(n, "init_mm") == 0) { init_mm_addr = a; break; }
        }
    }
    fclose(f);
    if (!init_mm_addr) return 0;
    
    /* init_mm is a struct mm_struct. On ARM 32-bit, pgd is the first field
     * that's a pointer. Actually it's at offset depending on kernel version.
     * struct mm_struct { ... pgd_t *pgd; ... }
     * On 5.4 ARM, pgd is at offset ~40-48 bytes typically.
     * Let's just read the known swapper_pg_dir location instead.
     *
     * On ARM, swapper_pg_dir is typically at PAGE_OFFSET + 0x4000
     * or init_mm.pgd points to it.
     */
    
    /* Try reading pgd from init_mm struct */
    unsigned long init_mm_phys = VIRT_TO_PHYS_LINEAR(init_mm_addr);
    printf("init_mm virt=0x%lx phys=0x%lx\n", init_mm_addr, init_mm_phys);
    
    /* Read several offsets to find the pgd pointer */
    for (int off = 0; off < 80; off += 4) {
        uint32_t val = read_phys32(init_mm_phys + off);
        /* pgd should be a kernel virtual address starting with 0x80 */
        if ((val & 0xFF000000) == 0x80000000) {
            /* Could be pgd - verify it points to valid page table */
            unsigned long pgd_phys = VIRT_TO_PHYS_LINEAR(val);
            uint32_t first_entry = read_phys32(pgd_phys);
            if (first_entry != 0) {
                printf("  init_mm+%d = 0x%08x (phys 0x%lx, first_entry=0x%08x) <- possible pgd\n", 
                       off, val, pgd_phys, first_entry);
            }
        }
    }
    
    /* Common location: check if swapper_pg_dir is at 0x80004000 */
    unsigned long common_pgd = VIRT_TO_PHYS_LINEAR(0x80004000);
    uint32_t test = read_phys32(common_pgd);
    printf("swapper_pg_dir guess (0x80004000 -> phys 0x%lx) = 0x%08x\n", common_pgd, test);
    
    return 0; /* Will figure out from output */
}

static unsigned long virt_to_phys_walk(unsigned long pgd_phys, unsigned long virt) {
    unsigned long l1_idx = (virt >> 20) & 0xFFF;
    unsigned long l1_pa = pgd_phys + l1_idx * 4;
    uint32_t l1 = read_phys32(l1_pa);
    printf("  L1[%lu] @ 0x%lx = 0x%08x ", l1_idx, l1_pa, l1);
    
    if ((l1 & 0x3) == 0x1) {
        printf("(page table)\n");
        unsigned long l2_base = l1 & 0xFFFFFC00;
        unsigned long l2_idx = (virt >> 12) & 0xFF;
        uint32_t l2 = read_phys32(l2_base + l2_idx * 4);
        printf("  L2[%lu] @ 0x%lx = 0x%08x ", l2_idx, l2_base + l2_idx*4, l2);
        if ((l2 & 0x2) == 0x2) {
            unsigned long phys = (l2 & 0xFFFFF000) | (virt & 0xFFF);
            printf("(small page) -> phys 0x%lx\n", phys);
            return phys;
        }
        printf("(unknown L2 type)\n");
    } else if ((l1 & 0x3) == 0x2) {
        unsigned long phys = (l1 & 0xFFF00000) | (virt & 0xFFFFF);
        printf("(section) -> phys 0x%lx\n", phys);
        return phys;
    } else {
        printf("(invalid/unmapped)\n");
    }
    return 0;
}

int main(int argc, char *argv[]) {
    if (argc < 3) {
        printf("Usage: %s <read|patch|findpgd> <virt_addr_hex> [pgd_phys_hex]\n", argv[0]);
        return 1;
    }
    
    memfd = open("/dev/mem", O_RDWR | O_SYNC);
    if (memfd < 0) { perror("/dev/mem"); return 1; }
    
    char *cmd = argv[1];
    unsigned long virt = strtoul(argv[2], NULL, 16);
    
    if (strcmp(cmd, "findpgd") == 0) {
        find_pgd_phys();
        close(memfd);
        return 0;
    }
    
    if (argc < 4) {
        printf("Need pgd_phys_hex. Run: %s findpgd 0 first\n", argv[0]);
        close(memfd);
        return 1;
    }
    
    unsigned long pgd_phys = strtoul(argv[3], NULL, 16);
    printf("PGD phys: 0x%lx\n", pgd_phys);
    printf("Walking virt 0x%lx:\n", virt);
    
    unsigned long phys = virt_to_phys_walk(pgd_phys, virt);
    if (!phys) { printf("Translation failed\n"); close(memfd); return 1; }
    
    printf("Result: virt 0x%lx -> phys 0x%lx\n", virt, phys);
    
    if (strcmp(cmd, "read") == 0) {
        for (int i = 0; i < 8; i++) {
            uint32_t val = read_phys32(phys + i*4);
            printf("  0x%lx: 0x%08x\n", virt + i*4, val);
        }
    } else if (strcmp(cmd, "patch") == 0) {
        uint32_t cur0 = read_phys32(phys);
        uint32_t cur1 = read_phys32(phys + 4);
        printf("Current:     0x%08x 0x%08x\n", cur0, cur1);
        printf("Patching to: 0xe3a00000 0xe12fff1e (mov r0,#0; bx lr)\n");
        
        write_phys32(phys, 0xE3A00000);
        write_phys32(phys + 4, 0xE12FFF1E);
        
        uint32_t v0 = read_phys32(phys);
        uint32_t v1 = read_phys32(phys + 4);
        printf("Verify:      0x%08x 0x%08x\n", v0, v1);
        printf("%s\n", (v0 == 0xE3A00000 && v1 == 0xE12FFF1E) ? "PATCHED OK!" : "PATCH FAILED");
    }
    
    close(memfd);
    return 0;
}

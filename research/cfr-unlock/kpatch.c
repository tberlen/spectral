#include <stdio.h>
#include <stdlib.h>
#include <fcntl.h>
#include <unistd.h>
#include <string.h>
#include <sys/mman.h>

/* 
 * ARM instructions for "return 0":
 * mov r0, #0    -> 0xE3A00000
 * bx lr         -> 0xE12FFF1E
 */

int main(int argc, char *argv[]) {
    if (argc < 2) {
        printf("Usage: %s <read|patch> [address_hex]\n", argv[0]);
        printf("  read  0x7fb14ba8  - read 32 bytes at address\n");
        printf("  patch 0x7fb14ba8  - patch function to return 0\n");
        return 1;
    }
    
    char *cmd = argv[1];
    unsigned long addr = 0;
    if (argc >= 3) addr = strtoul(argv[2], NULL, 16);
    
    int fd = open("/dev/kmem", O_RDWR);
    if (fd < 0) {
        /* Try /dev/mem with direct physical mapping - won't work for vmalloc */
        perror("open /dev/kmem");
        
        /* Alternative: write via /proc/kcore or direct kernel mem */
        /* Try opening the umac module file and patching via module interface */
        printf("Trying alternative approach via /proc/kallsyms lookup...\n");
        
        /* Last resort: use the module's own memory by loading through kallsyms */
        fd = open("/dev/mem", O_RDWR | O_SYNC);
        if (fd < 0) {
            perror("open /dev/mem");
            return 1;
        }
        printf("Warning: /dev/mem opened, but vmalloc addresses may not be accessible\n");
    }
    
    if (strcmp(cmd, "read") == 0 && addr) {
        /* Try to seek and read */
        off_t off = lseek(fd, addr, SEEK_SET);
        if (off == (off_t)-1) {
            perror("lseek");
            /* Try mmap */
            unsigned long page = addr & ~0xFFF;
            unsigned long offset = addr & 0xFFF;
            void *map = mmap(NULL, 4096, PROT_READ, MAP_SHARED, fd, page);
            if (map == MAP_FAILED) {
                perror("mmap");
                close(fd);
                return 1;
            }
            unsigned char *p = (unsigned char*)map + offset;
            printf("Bytes at 0x%lx:\n", addr);
            for (int i = 0; i < 32; i++) {
                printf("%02x ", p[i]);
                if ((i+1) % 16 == 0) printf("\n");
            }
            printf("\n");
            
            /* Decode as ARM instructions */
            unsigned int *instr = (unsigned int*)((unsigned char*)map + offset);
            for (int i = 0; i < 8; i++) {
                printf("  0x%lx: 0x%08x\n", addr + i*4, instr[i]);
            }
            munmap(map, 4096);
        } else {
            unsigned char buf[32];
            int n = read(fd, buf, 32);
            printf("Read %d bytes at 0x%lx:\n", n, addr);
            for (int i = 0; i < n; i++) {
                printf("%02x ", buf[i]);
                if ((i+1) % 16 == 0) printf("\n");
            }
            printf("\n");
        }
    }
    else if (strcmp(cmd, "patch") == 0 && addr) {
        /* Patch: write "mov r0, #0; bx lr" */
        unsigned int patch[2] = { 0xE3A00000, 0xE12FFF1E };
        
        off_t off = lseek(fd, addr, SEEK_SET);
        if (off == (off_t)-1) {
            /* Try mmap */
            unsigned long page = addr & ~0xFFF;
            unsigned long offset = addr & 0xFFF;
            void *map = mmap(NULL, 4096, PROT_READ | PROT_WRITE, MAP_SHARED, fd, page);
            if (map == MAP_FAILED) {
                perror("mmap for write");
                close(fd);
                return 1;
            }
            
            /* Read current */
            unsigned int *p = (unsigned int*)((unsigned char*)map + offset);
            printf("Current:  0x%08x 0x%08x\n", p[0], p[1]);
            printf("Patching to: 0x%08x 0x%08x (mov r0,#0; bx lr)\n", patch[0], patch[1]);
            
            p[0] = patch[0];
            p[1] = patch[1];
            
            /* Verify */
            printf("Verify:   0x%08x 0x%08x\n", p[0], p[1]);
            
            munmap(map, 4096);
            printf("PATCHED OK\n");
        } else {
            /* Read current */
            unsigned int cur[2];
            read(fd, cur, 8);
            printf("Current:  0x%08x 0x%08x\n", cur[0], cur[1]);
            
            lseek(fd, addr, SEEK_SET);
            printf("Patching to: 0x%08x 0x%08x (mov r0,#0; bx lr)\n", patch[0], patch[1]);
            int n = write(fd, patch, 8);
            printf("Wrote %d bytes\n", n);
            
            /* Verify */
            lseek(fd, addr, SEEK_SET);
            read(fd, cur, 8);
            printf("Verify:   0x%08x 0x%08x\n", cur[0], cur[1]);
            
            if (cur[0] == patch[0] && cur[1] == patch[1])
                printf("PATCHED OK\n");
            else
                printf("PATCH FAILED - verify mismatch\n");
        }
    }
    
    close(fd);
    return 0;
}

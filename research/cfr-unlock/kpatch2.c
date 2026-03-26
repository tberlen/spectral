#include <stdio.h>
#include <stdlib.h>
#include <fcntl.h>
#include <unistd.h>
#include <string.h>
#include <sys/mman.h>
#include <errno.h>

int main(int argc, char *argv[]) {
    if (argc < 3) {
        printf("Usage: %s <read|patch> <address_hex>\n", argv[0]);
        return 1;
    }
    
    char *cmd = argv[1];
    unsigned long addr = strtoul(argv[2], NULL, 16);
    unsigned long page = addr & ~0xFFFUL;
    unsigned long offset = addr & 0xFFF;
    
    printf("Address: 0x%lx (page: 0x%lx, offset: 0x%lx)\n", addr, page, offset);
    
    /* Try /dev/kmem first for virtual addresses */
    int fd = open("/dev/kmem", O_RDWR);
    char *devname = "/dev/kmem";
    if (fd < 0) {
        printf("/dev/kmem failed: %s\n", strerror(errno));
        fd = open("/dev/mem", O_RDWR | O_SYNC);
        devname = "/dev/mem";
        if (fd < 0) {
            printf("/dev/mem failed: %s\n", strerror(errno));
            return 1;
        }
    }
    printf("Opened %s (fd=%d)\n", devname, fd);
    
    /* Try mmap */
    void *map = mmap(NULL, 4096, PROT_READ | PROT_WRITE, MAP_SHARED, fd, page);
    if (map == MAP_FAILED) {
        printf("mmap at page 0x%lx failed: %s\n", page, strerror(errno));
        
        /* If /dev/kmem mmap failed, try /dev/mem */
        if (strcmp(devname, "/dev/kmem") == 0) {
            close(fd);
            fd = open("/dev/mem", O_RDWR | O_SYNC);
            if (fd >= 0) {
                printf("Trying /dev/mem mmap...\n");
                map = mmap(NULL, 4096, PROT_READ | PROT_WRITE, MAP_SHARED, fd, page);
                if (map == MAP_FAILED) {
                    printf("/dev/mem mmap also failed: %s\n", strerror(errno));
                    close(fd);
                    return 1;
                }
                devname = "/dev/mem";
            }
        } else {
            close(fd);
            return 1;
        }
    }
    printf("mmap OK via %s\n", devname);
    
    unsigned int *p = (unsigned int*)((unsigned char*)map + offset);
    
    if (strcmp(cmd, "read") == 0) {
        printf("Instructions at 0x%lx:\n", addr);
        for (int i = 0; i < 8; i++) {
            printf("  0x%lx: 0x%08x\n", addr + i*4, p[i]);
        }
    }
    else if (strcmp(cmd, "patch") == 0) {
        unsigned int patch[2] = { 0xE3A00000, 0xE12FFF1E };
        printf("Current:     0x%08x 0x%08x\n", p[0], p[1]);
        printf("Patching to: 0x%08x 0x%08x (mov r0,#0; bx lr)\n", patch[0], patch[1]);
        
        p[0] = patch[0];
        p[1] = patch[1];
        
        /* Flush cache */
        __builtin___clear_cache((char*)p, (char*)p + 8);
        
        printf("Verify:      0x%08x 0x%08x\n", p[0], p[1]);
        
        if (p[0] == patch[0] && p[1] == patch[1])
            printf("PATCHED OK\n");
        else
            printf("PATCH FAILED\n");
    }
    
    munmap(map, 4096);
    close(fd);
    return 0;
}

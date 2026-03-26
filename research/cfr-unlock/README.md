# CFR (Channel Frequency Response) Unlock Research

## Summary

CFR is essentially CSI (Channel State Information) - per-subcarrier amplitude and phase data that provides much higher fidelity than spectral FFT. The entire CFR software stack exists on UniFi APs but is disabled by a firmware feature bit.

## What We Found

### CFR Code Exists End-to-End

**Kernel Symbols (from `/proc/kallsyms`):**
```
wlan_cfr_is_feature_disabled     [umac]     <- THE GATE
wlan_cfr_is_ini_disabled         [umac]
ucfg_cfr_start_capture           [umac]
ucfg_cfr_stop_capture            [umac]
ucfg_cfr_set_capture_count       [umac]
ucfg_cfr_set_capture_interval    [umac]
ucfg_cfr_set_capture_duration    [umac]
ucfg_cfr_set_bw_nss              [umac]
ucfg_cfr_set_frame_type_subtype  [umac]
ucfg_cfr_set_tara_config         [umac]    (TA/RA filter)
ucfg_cfr_set_en_bitmap           [umac]
ucfg_cfr_config_rcc              [umac]    (Receive Chain Capture)
ucfg_cfr_get_rcc_enabled         [umac]
send_peer_cfr_capture_cmd_tlv    [wifi_3_0]
send_cfr_rcc_cmd_tlv             [wifi_3_0]
cfr_dbr_event_handler            [qca_ol]  (Direct Buffer Ring handler)
enh_cfr_dbr_event_handler        [qca_ol]  (Enhanced CFR handler)
target_if_cfr_init_pdev          [qca_ol]
target_if_cfr_start_capture      [qca_ol]
target_if_cfr_config_rcc         [qca_ol]
target_if_cfr_periodic_peer_cfr_enable [qca_ol]
init_deinit_cfr_support_enable   [qca_ol]
dp_rx_handle_cfr                 [monitor]
dp_rx_mon_populate_cfr_info      [monitor]
```

### DebugFS Files Present
```
/sys/kernel/debug/qdf/cfrwifi0/cfr_dump0   <- CFR data relay file (per radio)
/sys/kernel/debug/qdf/cfrwifi1/cfr_dump0
/sys/kernel/debug/qdf/cfrwifi2/cfr_dump0
/sys/kernel/debug/qdf/dbr_ring_debug/      <- Direct Buffer Ring (used for CFR)
```

### CFR Test App Exists
```
/usr/sbin/cfr_test_app -i <interfacename>
```
Reads from `/sys/kernel/debug/qdf/cfr%s/cfr_dump0` and writes to `/tmp/cfr_dump_<iface>_<timestamp>.bin`. This is Qualcomm's reference CFR capture tool.

### cfg80211tool CFR Commands
```
cfg80211tool wifi0 cfr_timer <value>          <- Set capture timer
cfg80211tool wifi0 get_cfr_timer              <- Get timer
cfg80211tool wifi0 get_cfr_capture_status     <- Check status (always returns 0)
```

## What's Blocking It

### The Gate Function
`wlan_cfr_is_feature_disabled` in the `umac` module returns `true`, which causes ALL CFR commands to be rejected with error -22 (EINVAL).

This function checks the **firmware service capability bits** that the radio firmware advertises during initialization. The Ubiquiti firmware blob does not set the CFR support bit.

### Error Chain
```
User runs: cfg80211tool wifi0 cfr_timer 100
  -> cfg80211 sends NL80211 command
    -> wlan_cfg80211_cfr_params() in umac
      -> wlan_cfr_is_feature_disabled() returns TRUE
        -> Returns -EINVAL (-22)
```

### Evidence
- `cfg80211tool wifi0 cfr_timer 1` succeeds (value 1 may be a query/noop)
- `cfg80211tool wifi0 cfr_timer 10` fails with -22
- `get_cfr_capture_status` always returns 0
- `cfr_test_app -i wifi1` starts but captures 0 bytes
- All debugfs cfr_dump files remain empty (0 bytes)

## What We Tried

### 1. cfg80211tool Commands
All CFR-related commands return -22 (EINVAL) because the feature check fails.

### 2. wifitool beeliner_fw_test
```
wifitool wifi1ap3 beeliner_fw_test 107 1
```
Returns success (rc=0) but doesn't actually enable CFR. Command 107 may not be the CFR enable command.

### 3. Direct Memory Patching via /dev/mem
Attempted to overwrite `wlan_cfr_is_feature_disabled` (at virtual address 0x7fb14ba8 in umac module) to always return 0.

**Problem:** Module memory is in vmalloc space. `/dev/mem` only provides access to physical addresses. Translating vmalloc virtual addresses to physical requires walking the kernel page tables, but:
- `swapper_pg_dir` is not exported in `/proc/kallsyms`
- We could not locate the physical address of the page tables
- ARM page table walk via devmem was unsuccessful

### 4. /dev/kmem
Exists but `mmap()` returns "Input/output error" for vmalloc addresses. `lseek()`+`read()` returns -1.

### 5. Kernel Module Loading
`insmod` is available but we cannot compile kernel modules without the matching kernel headers for 5.4.164 on IPQ5018.

### 6. kprobes / ftrace / livepatch
All disabled/stripped by Ubiquiti.

## Paths Forward

### Most Likely to Succeed

1. **Build matching kernel module**
   - Need QSDK for IPQ5018 with kernel 5.4.164
   - Build a tiny .ko that uses `kallsyms_lookup_name()` to find `wlan_cfr_is_feature_disabled` and patches it in kernel space
   - vermagic must match: `5.4.164 SMP preempt mod_unload ARMv7 p2v8`
   - Platform: `IPQ5018/AP-MP03.1`

2. **ESP32-C5 external CSI capture**
   - Already proven in the barn project (see-you)
   - $8 per board, captures CSI from AP beacon frames
   - No AP modification needed
   - Firmware already exists

3. **OpenWrt with ath11k**
   - IPQ5018 is supported by ath11k in OpenWrt
   - ath11k has CFR support in the open-source driver
   - Would require replacing Ubiquiti firmware entirely (destructive)

### Long Shots

4. **Find the page table physical address**
   - Could try brute-force scanning physical memory for valid page table structures
   - Risky - reading wrong addresses could hang the system

5. **Firmware modification**
   - Modify the Qualcomm firmware blob to set the CFR service bit
   - Extremely difficult without firmware documentation

6. **Ubiquiti feature request**
   - Ubiquiti could enable CFR in a firmware update
   - Unlikely without significant customer demand

## Test Environment

- **AP:** Garage-AP (home lab)
- **Model:** U6-IW
- **IP:** REDACTED_LAB_IP
- **SSH:** REDACTED_SSH_USER / REDACTED_SSH_PASS
- **SoC:** IPQ5018 (Maple) 1.1
- **Board ID:** ap-mp03.1
- **Kernel:** 5.4.164 SMP preempt ARMv7
- **Physical memory:** 0x41000000-0x4AAFFFFF, 0x52200000-0x7FFF7FFF
- **Kernel virt-to-phys offset:** 0x3F000000 (virt 0x80300000 = phys 0x41300000)
- **WiFi radios:** wifi0 (2.4GHz IPQ5018), wifi1 (5GHz QCN9000 PCIe), wifi2 (6GHz QCN9000 PCIe)
- **umac module base:** 0x7F9F8000 (virtual)
- **Target function:** `wlan_cfr_is_feature_disabled` at 0x7FB14BA8 (virtual, in umac)

## Files

- `kpatch.c` - First attempt at /dev/kmem patcher
- `kpatch2.c` - Improved patcher with mmap fallback
- `cfr_patch.c` - Page table walking patcher (couldn't find swapper_pg_dir)
- `cfr_enable.c` - Kernel module source (needs matching headers to compile)

## BREAKTHROUGH: Kernel Module Built and Loaded Successfully

### Build Environment
- **Kernel source:** linux-5.4.164 from kernel.org
- **Kernel config:** Extracted from AP via `/proc/config.gz`
- **Compiler:** arm-linux-gnueabi-gcc 12.2.0 (on Debian LXC)
- **Build command:** `make -C /tmp/linux-5.4.164 M=/tmp ARCH=arm CROSS_COMPILE=arm-linux-gnueabi- modules`
- **Module size:** ~6KB
- **Key:** `CONFIG_MODVERSIONS` is NOT set, so only vermagic needs to match

### What Worked
1. **Module loaded via `insmod`** - vermagic matched perfectly
2. **`wlan_cfr_is_feature_disabled` patched** - function now returns 0
3. **`cfr_timer 1` now succeeds** (previously returned -22)
4. **`tgt_cfr_support_set()` called** - marked CFR as supported
5. **`cfr_initialize_pdev()` called** - attempted full CFR init

### What Failed
CFR initialization hits a hardware limitation:
```
target_if_dbr_init_ring: srng setup failed
target_if_direct_buf_rx_module_register: init dbr ring fail, srng_id 0, status 16
cfr_enh_init_pdev: Failed to register with dbr
```

The **Direct Buffer Ring (DBR)** - the DMA ring that carries CFR data from firmware to host memory - cannot be initialized after boot. The radio firmware configures its DMA rings during initialization based on advertised capabilities. Since CFR wasn't advertised, no DBR ring was allocated for it.

### Conclusion
The host-side software is fully unlockable. The blocker is the **firmware's DMA ring allocation** which happens at boot time and cannot be reconfigured. To get CFR working, the firmware itself would need to:
1. Advertise CFR as a supported service during WMI init
2. Allocate a DBR ring for CFR data during ring setup

This is a firmware-level change, not a host/driver change.

### Build Instructions
```bash
# On a machine with arm-linux-gnueabi-gcc:
tar xf linux-5.4.164.tar.xz
cp kernel_config.txt linux-5.4.164/.config
cd linux-5.4.164
make ARCH=arm CROSS_COMPILE=arm-linux-gnueabi- olddefconfig
make ARCH=arm CROSS_COMPILE=arm-linux-gnueabi- modules_prepare

# Build the module
cd /path/to/cfr_enable.c
make -C /path/to/linux-5.4.164 M=$(pwd) ARCH=arm CROSS_COMPILE=arm-linux-gnueabi- modules

# Deploy to AP
base64 cfr_enable.ko | ssh user@ap 'base64 -d > /tmp/cfr_enable.ko'
ssh user@ap 'insmod /tmp/cfr_enable.ko'
```

## v6: Init Intercept Approach

### What v6 Does
Patches TWO functions:
1. `wlan_cfr_is_feature_disabled` -> returns 0 (bypasses feature check)
2. `init_deinit_cfr_support_enable` -> calls `target_if_cfr_set_cfr_support(psoc, 1)` directly

The second patch builds an ARM branch instruction at runtime:
```
mov r1, #1                          @ CFR support = true
b   target_if_cfr_set_cfr_support   @ tail call
```

### Result
Both patches apply successfully. However, `wifi down && wifi up` doesn't properly restart the radio firmware on Ubiquiti's platform (their `wifi` script relies on `uci` which isn't configured).

### The DBR Ring Problem (Deeper Understanding)
The firmware on the QCN9000 PCIe radio chip runs independently. Host driver restart doesn't restart the radio firmware. The DBR (Direct Buffer Ring) is a DMA ring allocated by the firmware during its boot, based on what services it plans to offer. Since the firmware doesn't advertise CFR:
- No DBR ring is allocated for CFR data
- The host-side `cfr_enh_init_pdev` tries to register a DBR consumer for CFR
- Registration fails because no ring exists: `target_if_dbr_init_ring: srng setup failed`

### Next Steps to Try
1. **Full PCIe device reset** - reset the QCN9000 via PCIe, force firmware reload with patches active
2. **Modify amss.bin** - find the service bitmap in the firmware binary, flip the CFR bit, then reboot
3. **WMI command injection** - send a raw WMI command to the firmware requesting CFR DBR ring allocation
4. **fw_ini_cfg.bin modification** - the firmware reads this JSON config; maybe a DBR ring count parameter exists

### Key Firmware Strings Found
```
wlan_cfr.c                    - CFR source file compiled into firmware
whal_cfir_configure: rtt %d capture_cfr %d capture_cir %d  - CFR configure function
whal_cfir_enable: enaRttPerBurst %d ...  - CFR enable function
wal_cfir_resolve: pdev %d cfg 0x%08x ...  - CFR resolution
CFR/CIR report queue full      - CFR data queue management
```

The firmware has full CFR code. The only thing missing is the service advertisement at boot.

## v7: Service Bitmap Injection (CURRENT APPROACH)

### Key Insight
The DBR ring allocation is controlled by the **HOST**, not the firmware. The sequence is:
1. Firmware sends WMI_SERVICE_READY (without CFR bit)
2. Host saves the service bitmap
3. Host sends WMI_INIT_CMD requesting resources for supported services
4. **Firmware allocates resources based on HOST's request**

If we inject CFR support into the saved bitmap BEFORE WMI_INIT_CMD is sent,
the firmware should allocate a CFR DBR ring because the host requested it.

### v7 Module
- Patches `wlan_cfr_is_feature_disabled` -> return 0
- Patches `init_deinit_cfr_support_enable` -> force `target_if_cfr_set_cfr_support(psoc, 1)`
- Both patches verified working

### Remaining Issue
Need to properly restart the wifi stack with patches active. Ubiquiti's `wifi` script
uses UCI which isn't configured on their platform. Need to find their internal
radio restart mechanism (possibly `mca-cli-op` or module unload/reload).

### Firmware Analysis
- `amss.bin` is ELF 32-bit ARM, 3.9MB
- Contains CFR code: wlan_cfr.c, whal_cfir.c, phyCfrCirCap.c, wal_cfir.c
- `IMAGE_VARIANT_STRING=9000.wlanfw.eval_v1Q` - evaluation firmware
- `phyCfrCirCap.c` has "Enable CFR ini programming" - firmware reads an INI param for CFR
- `CFR_UNIT_TEST_CMD` interface exists in firmware for testing CFR
- Firmware sends WMI_SERVICE_READY with capabilities to host
- Host decides what to request in WMI_INIT_CMD

### Next Steps
1. Find proper wifi restart on Ubiquiti platform (reboot AP with module in init)
2. Or: patch `save_service_bitmap_tlv` to inject bit 142 into bitmap
3. Test if firmware honors CFR ring request when CFR wasn't originally advertised

# Colony OS · 内核全景设计与冻结契约规范（v2）

> 代号：`K`（Kernel Specification）。上位依据：`00-理念与总纲.md`（`I`）、`01-Colony系统架构设计.md`（`A`）。
>
> **本规范承担两项死任务**：
> 1. **按终局完整全景一次把内核定死**：哪怕第一阶段只演示「路由＋音箱＋Kindle＋电脑互联跑模型」，内核的数据结构、线协议、信任环、零拷贝数据面、内容寻址块仓、电源与硬实时隔离也**必须从第一天就完整容纳第二/三阶段的「50GB 独立游戏 P2P 边下边玩、掌机 120FPS 独占直通、电池能效管控、陌生创作者防投毒、好友点对点联机、车机实时控制与多智能体高并发沙箱」**——上层增量分阶段长，内核 ABI 十年不破；
> 2. **清楚到 Flash 级模型（如 DeepSeek 快速模型）全自动跑到底零失误**：消灭一切「自行设计合适结构」「妥善处理异常」之类模糊话术。每个子模块的依赖边界、字节帧排布、结构体字段、有限状态机（FSM）、错误码与机械验收断言在本规范中全部唯一锁死。

---

## §一、终局全景压力矩阵：内核到底要扛住哪十种极端场景？

判断一个内核设计有没有偷懒，就看下面十种跨越十年终局的真实场景同时压上来时，内核需不需要改协议或打脏补丁。本规范的每一处原语都直接对齐这十道考题：

| 考题编号 | 终局真实场景 | 如果只按「第一阶段够用」设计会怎么死 | 本规范在内核里一次埋好的原语 |
|---|---|---|---|
| **S-01** | **64MB 内存老 Kindle / 百元旧路由原生装机**（保厂商内核作硬件层：老 Linux 3.x/4.x，缺 `cgroup v2`、无硬件 AES 指令） | 启动直接因缺内核特性崩溃；或引了 OpenSSL 交叉编译不过、加密卡死 CPU | `HostCaps` 启动探测＋三档隔离优雅降级（§三）；全栈纯软件极速密码学 `ChaCha20-Poly1305 + BLAKE3 + Ed25519`（§四） |
| **S-02** | **同一账号跨局域网与异地移动网络秒级成圈**（家里 NAS＋办公室电脑＋随身掌机/Kindle） | 只写了局域网广播，一出门全断；或者偷懒做成中心化云服务器中转明文，背叛数据主权 | `Ring 0` 主权账号根派生＋mDNS 直连＋零留存寻址信标 NAT 打洞/端到端加密中继（§四） |
| **S-03** | **第二阶段与全球陌生人 P2P 交换 50GB 独立游戏包、核验独立制作人作品签名** | 只有「自己人全信任网」概念，一接外部 P2P 节点等于把陌生人放进家庭内网 | K-2 原生**两半场物理隔离**（`Ring 0 内网织体` / `Ring 1 对外发布面`），外部连接只能打到你发布的节点，严禁触碰内部能力总线（§四） |
| **S-04** | **独立游戏边下边玩、20 款游戏共享 Proton/引擎层不重复占盘、系统 `.cext` 原子挂载、GB 级模型权重校验** | 系统包一套格式、模型一套格式、游戏又造一套下载器，校验慢且磁盘爆炸 | K-3/K-4 统一 **256KB 分块 BLAKE3 Merkle 内容寻址底座（`b3://` CAS）**，流式逐块验真、天然跨包去重（§五） |
| **S-05** | **掌机 120FPS Vulkan 游戏渲染、音箱 20ms 实时双向语音、本地模型 `mmap` 零拷贝读权重** | 所有数据都塞进 IPC 消息帧让 `colonyd` 转发，CPU 拷爆、延迟几百毫秒 | K-5 **控制面与数据面彻底分离**：本机经 `SCM_RIGHTS` 传 `memfd / pipe / dmabuf` 句柄零拷贝直通；跨机走 4 级 QoS 独立加密流（§六） |
| **S-06** | **家里 NAS 一边给陌生人后台做种传游戏，客厅音箱同时在实时语音对话** | 后台大流量把网卡队列塞满，实时语音卡成电音 | K-5/K-2 隧道内置 **4 级硬件对齐 QoS 优先级队列**，`0=RT_Critical` 与 `1=Interactive` 绝对抢占 `3=Opportunistic_Swarm`（§六） |
| **S-07** | **掌机/Kindle 拔电用电池待机，或正全屏打大型独立游戏、车机正跑实时导航** | 后台小模型凝炼和 P2P 做种继续狂跑，半小时吸干掌机电池、把游戏卡掉帧 | K-6 **电源与独占实时调速闸（Power & Seat Governor）**：进电池低电或独占游戏席位时，1ms 内内核级 `cgroup.freeze` 冻结后台机会型任务（§七） |
| **S-08** | **CardColony 在机上全自动跑 20 个智能体，每个智能体又派生多层编译/脚本子进程** | 子进程逃逸、内存撑爆整机 OOM 卡死界面、点停止杀不干净留孤儿进程 | K-6 焊死 `System Reserve` 保底内存＋层级 `cgroup` 树租约＋`tree.kill` 原子全树清场（§七） |
| **S-09** | **用户发现某个蓝图节点或应用烦人，当场剪断授权线；或某台授权音箱丢失被吊销** | 已经建好的长连接和打开的文件句柄还在继续偷传数据 | K-7 **只减不增能力线（Attenuated Line）＋ 代次围栏（Epoch Fence）**，代次一跳立即原子掐断旧通道、封死共享内存并杀子租约（§八） |
| **S-10** | **刷在路由器或高处设备上的系统半夜升级时突然停电** | 闪存写一半变砖，普通用户救不回来 | K-3 双槽只读 `system_a/b` ＋ `PENDING_COMMIT` 启动计数看门狗，未签健康承诺断电重启自动退回旧槽（§五） |

---

## §二、面向 Flash 级模型的物理切分：9 个单职责无环子模块（Workspace DAG）

为了让参数量较小或高速模式的模型（如 DeepSeek）全自动写代码时**永远不需要同时在脑子里装超过 1200 行逻辑**，`kernel/colonyd` 在源码物理结构上强制拆分为 **1 个纯类型契约库 ＋ 8 个单向无环功能库 ＋ 1 个薄入口**。

### 2.1 严格单向依赖分层图（下层严禁反向引用上层，编译期静态门禁锁死）

```
第 4 层（组装入口，≤300行）:                  [ colonyd-bin ]
                                                    │
第 3 层（契约锚总线与路由，≤1200行）:         [ colony-bus ]  (K-5)
                                       ┌────────────┼────────────┬────────────┐
第 2 层（四大核心执行器官，互不横引）: [colony-ident] [colony-ext] [colony-sandbox] [colony-boot]
                                       (K-2 身份织体)  (K-4 海关)   (K-6 沙箱/调速)  (K-3 A/B槽)
                                       └────────────┴─────┬──────┴────────────┘
                                       ┌──────────────────┼──────────────────┐
第 1 层（三大纯底座原语，互不横引）:   [ colony-wire ]    [ colony-cas ]     [ colony-ledger ]
                                       (二进制帧编解码)   (BLAKE3分块仓)      (K-7 原子KV账本)
                                       └──────────────────┼──────────────────┘
第 0 层（零逻辑纯数据结构与错误码）:                  [ colony-abi ]
```

### 2.2 九个模块职责与「Flash 模型防越界禁令表」

| 模块名 | 对应内核号 | 只许干什么（唯一职责） | 严禁出现什么（静态扫描红线） | 独立单测硬判据 |
|---|---|---|---|---|
| `colony-abi` | K-5 宪章 | 定义所有结构体、枚举、常量、错误码、Trait 接口签名 | **严禁有任何 I/O、网络、文件、线程或异步运行时代码** | 所有类型序列化/反序列化黄金字节向量 100% 字节级相等 |
| `colony-wire` | K-5 线形 | 24 字节定长帧头编解码、确定性 MsgPack/CBOR 载荷编解码、Unix Socket `SCM_RIGHTS` 文件描述符收发封装 | 严禁查权限、严禁持久化、严禁懂业务方法名 | 10 万次随机畸形/截断字节流模糊测试零 panic，合法帧往返恒等 |
| `colony-cas` | K-3/K-4 块仓 | 256KB 分块 BLAKE3 Merkle 树计算、块写入、流式逐块校验读取、引用计数与垃圾回收 | 严禁碰网络下载（怎么拉块是上层的事，它只管验真和落盘） | 篡改任意 1 比特必报 `E_HASH_MISMATCH`；两份共享半数块的镜像落盘物理只占一份半空间 |
| `colony-ledger` | K-7 账本 | 单文件嵌入式事务 KV 库封装，维护 5 张内核铁账与单调递增代次号 `epoch`，崩溃原子恢复 | 严禁存大块内容或用户业务文本（单 Value 硬限 ≤ 16KB） | 随机 `kill -9` 注入 500 次后重开账本，已提交事务零丢失、未提交碎屑零残留 |
| `colony-boot` | K-3 生命线 | 探测引导槽位状态、读写健康标志、硬件/软件看门狗喂狗、签发健康承诺 `Commit` | 严禁在未通过 `self_check` 前置状态为 `HEALTHY` | 模拟连续 3 次启动崩溃未 Commit，槽位选择器确定性切回上一健康槽位 |
| `colony-ident` | K-2 身份织体 | 设备证书签发与验签、吊销名录对接（v1 主设备自持/v2 账号服务）、信任边界（Ring 0 内网 / Ring 1 发布面）分类、mDNS 发现、信标打洞、ChaCha20 加密多路流隧道、设备器官表广播 | 严禁把发布面连接路由进 Ring 0 路由表；严禁明文发任何载荷 | 伪造签名、过期证书、被吊销设备号、发布面越权访问内部方法 100% 拒止 |
| `colony-sandbox` | K-1/K-6 执法 | 启动时生成 `HostCaps`、创建三档隔离沙箱进程、管理 `cgroup` 五级目录与内存/CPU 限额、电源与独占席位自动冻结、设备直通句柄签发、`tree.kill` 全树强杀 | 严禁给受管进程默认开放外网 namespace；严禁挤占 `reserve` 保底池 | 子进程再 `fork` 孙进程后调 `tree.kill`，100ms 内整棵进程树存活数为 0 |
| `colony-ext` | K-4 海关 | 校验 `.cext` 签名与契约版本兼容、通过 `colony-cas` 准备只读挂载点（squashfs/erofs 或受控只读目录绑定）、向总线注册/注销能力提供者 | 严禁向系统只读区散写文件；卸载后挂载点残留必须为 0 | 坏签名拒装；新版启动挂死 1 秒内无损回切旧版 `.cext` |
| `colony-bus` | K-5 总线 | 唯一路由中枢：解析 `colony://` 地址 → 查 `colony-ledger` 验授权线与代次围栏 → 唤醒休眠居民 → 分发本机 IPC / 零拷贝 FD 或交 `colony-ident` 跨机转发 → 代次跳变时瞬时清场 | 严禁内置任何特定模型名、特定硬件名或特定应用逻辑 | 本机调用、跨机调用、休眠唤醒、剪线瞬时断流四条主路径全绿 |

---

## §三、K-1 宿主探测矩阵与「原生一刀切」装机规约

### 3.0 铁律：只收原生整机态，刷不了机的不进生态

- **唯一装机形态＝原生整机**：整台机器的用户态软件栈全归 Colony（主线内核优先；驱动焊死在厂商内核里的设备——如 Kindle 墨水屏——允许**保厂商内核纯当硬件抽象层**，其开机拉起的是 `colonyd`，原厂软件一律清场归零）；
- **刷不了机＝出局**：无法整机替换的锁死设备不进生态，档案库直接标「不可装」，不做观察、不留后门、不搞寄生；
- **微控制器/传感器不算装机**：ESP32、串口传感器这类无 Linux 能力的硬件由就近的 Colony 设备「收养」为硬件器官——它们是器官，不是公民，从来不需要装系统。

### 3.1 统一目录锚点（消灭 BSP 布局分叉）

`colonyd` 在原生整机态下作为 PID 1 或由极简 `colony-init` 拉起（主线/下游内核同一条路）。不同 BSP 的分区与闪存布局千差万别，内核内部所有模块只认相对于 `$COLONY_ROOT` 的四条标准逻辑锚点，物理挂载点由各 BSP 的装机配方决定：

| 逻辑锚点 | 典型物理挂载 | 读写与权限 |
|---|---|---|
| `$RUN_DIR` | `/run/colony` (`tmpfs`) | 存放 `anchor.sock` 与瞬态会话句柄 |
| `$SYS_DIR` | `/system`（当前激活的 `system_a` 或 `system_b` 只读分区） | **严格只读**，存放 `colonyd` 本体与基础器官适配器 |
| `$CAS_DIR` | `/extensions/cas` | `colonyd` 独占写，对居民只读映射，存 `.cext` / 模型块 / 游戏块 |
| `$DATA_DIR` | `/data`（加密分区） | 存放 `ledger.db`、本机密钥、各居民受控状态槽 |

> **布局降级规则**：闪存太小放不下 A/B 双槽的设备，BSP 配方改用「单槽＋恢复分区」模式——升级失败由恢复环境回退，而不是双槽切换。锚点抽象保证 `colonyd` 对这两种布局完全无感。

### 3.2 启动宿主能力探测结构体 `HostCaps`（精确定义）

`colony-sandbox` 在启动第 1 毫秒探测底层 Linux 内核，生成只读结构体 `HostCaps`，全系统据此自动选档，**严禁在代码里写机型硬编码**：

```rust
pub struct HostCaps {
    pub arch: Arch,                      // X86_64 | Aarch64 | Armv7 | Riscv64 | Mipsel
    pub kernel_lineage: KernelLineage,   // Mainline | Downstream（下游厂商内核——功能可能老旧，升级受限，但同样是原生整机态）
    pub total_ram_bytes: u64,            // 物理总内存字节数
    pub isolation_tier: IsolationTier,   // 自动判定的最高可用沙箱档位（见下表）
    pub has_cgroup_v2_freezer: bool,     // 是否支持 1ms 冻结后台任务树
    pub has_memfd: bool,                 // 是否支持匿名共享内存零拷贝（Linux >= 3.17）
    pub has_drm_kms: bool,               // 是否有显卡/帧缓冲独占席位能力
    pub has_fbdev_eink: bool,            // 是否有电子墨水屏/基础帧缓冲 (/dev/fb0)
    pub has_vulkan: bool,                // 是否具备硬件 Vulkan 渲染节点 (/dev/dri/renderD*)
    pub has_unified_vram: bool,          // 是否为统一内存架构（如掌机APU/SoC，渲染显存与系统内存共池，须联动硬限）
    pub dedicated_vram_bytes: u64,       // 若为独立显卡，独显物理显存字节数（统一内存时为 0）
    pub has_npu_accel: bool,             // 是否具备独立 NPU 推理加速设备节点（游戏占满 GPU 时小模型可直走 NPU！）
    pub has_alsa_audio: bool,            // 是否具备音频录放硬件 (/dev/snd/*)
    pub power_supply_kind: PowerKind,    // AlwaysAC（路由/NAS/台式） | BatteryBacked（掌机/Kindle/手机/车机）
}
```

### 3.3 三档沙箱优雅降级表（`IsolationTier`）

| 档位枚举 | 判定条件 | 执行的物理隔离手段 | 典型命中硬件 |
|---|---|---|---|
| `Tier1_Full` | Linux ≥ 5.4，具备 `cgroups v2` + `mount/pid/net/ipc namespaces` + `seccomp` | 完整独立挂载视图、空网络命名空间（100% 物理断外网）、硬内存上限 `memory.max`、硬进程树冻结与强杀 | 裸机 Colony 镜像、PC、NAS、掌机、现代 OpenWrt 路由 |
| `Tier2_Namespace` | 具备 `namespaces` 但无完整 `cgroup v2`（如老 `cgroup v1`） | 独立挂载与空网络命名空间物理断网；内存上限由 `setrlimit(RLIMIT_AS)` ＋ `colonyd` RSS 监控兜底；按 PID 命名空间整树清杀 | 部分 4.x 内核的旧 NAS、开发板、早期智能音箱 |
| `Tier3_LegacyPosix` | 古老锁定内核（如 Linux 3.x Kindle），无 namespace | `chroot` 独立目录围栏 ＋ 独立无特权 UID/GID ＋ `PR_SET_NO_NEW_PRIVS` ＋ `setrlimit` 硬限 ＋ 进程组 `PGID` 追踪强杀；仅允许运行经主设备签名的受信任轻量器官插件，拒装未授信第三方外部应用 | 越狱老款 Kindle、极老旧百元家电芯片 |

### 3.4 内核版本兼容策略（必需集＝上古原语，现代特性＝探测式启用）

Colony 对 Linux 内核版本的友好是**设计出来的**，不是运气：

1. **必需集＝上古原语**：`colonyd` 生存只需 POSIX 时代的东西——静态单文件（无 glibc/systemd 依赖）、`epoll` 事件驱动、unix socket、`fork/exec`、纯软件密码学（ChaCha20/BLAKE3/Ed25519 不借内核 crypto、不借 AES 指令）；跨机加密隧道是自建 Noise over UDP，**不依赖内核 WireGuard**。理论可运行底线 ≈ Linux 3.x（含厂商下游内核）。
2. **现代特性＝探测式启用，降级不拒绝**：每个新内核特性都是 `HostCaps` 里的一个 feature flag——有就赚、没有走退路，**绝不允许因缺特性而拒绝启动**：
   - `memfd`（3.17+）缺 → tmpfs 文件＋`SCM_RIGHTS` 传 fd，零拷贝照跑；
   - `cgroup v2`（4.5+）缺 → 降 Tier2（cgroup v1）→ 再降 Tier3（UID＋rlimit POSIX 保底）；
   - `seccomp`（3.5+）缺 → Tier3：no_new_privs＋只许签名插件；
   - `overlayfs`（3.18+）缺 → 只读目录绑定；
   - `landlock`（5.13+）缺 → 静默跳过。
3. **两张成绩单分开报**：「能不能跑」（必需集满足即可，几乎人人及格）与「保护有多硬」（Tier1/2/3 档位）是两张独立成绩单——`HostCaps` 如实汇报档位，设备档案库给每台机器标的就是这个真实水位，用户装之前就知道这台机器的笼子有多牢。
4. **工程底线＝Linux 3.x**：测试矩阵只承诺覆盖到 Linux 3.x（含下游厂商内核）；更老的（如 2.6.x 上古机）由档案库标「不可刷」——不是跑不动，是不为尾部 1% 设备背测试债。

---

## §四、K-2 账号锚定身份、信任边界与全自动织体协议

> **身份锚（已定·分两版）**：账号＝首台设备上自举生成的一对 Ed25519 根密钥——`account_pub` 即 Colony ID，终生不变，格式跨版不动。
> - **v1（开源 OS 首发）**：纯自举身份，零服务依赖——主设备持根密钥任发证机关＋吊销名录持有者，恢复靠离线密语＋自家设备加密分片，信标可由主设备/社区节点兼任。极客开箱即用，不碰任何官方服务。
> - **v2（随发现网络一起上）**：官方账号服务接入——手机号＋实名 KYC 绑定 `account_pub`，服务接管绑定名录、吊销目录、公共信标与零知识加密备份。铁律不变：**它知道你是谁，永远看不到你干什么**。`account_pub` 不换，v1 已发证书全部继续有效——服务接管的是"目录与便利"，密钥始终是你的。
> 生态层的账本/产权/辖区治理见其延申文档（`03`，下一阶段），内核只留接缝。

### 4.1 固定密码学套件（全平台零 C 依赖极速组合）

为保证从 64MB ARMv7 到 64GB PC 二进制互通，全系统唯一合法密码学原语固定为（不搞算法协商复杂度）：
- **签名算法**：`Ed25519`（公钥 32 字节，签名 64 字节）
- **密钥交换與隧道握手**：基于 `X25519` 的 **Noise_IK_25519_ChaChaPoly_BLAKE3** 双向认证握手（1-RTT 完成身份核验与前向安全会话密钥协商）
- **对称流加密**：`ChaCha20-Poly1305`（无硬件 AES 指令的老旧 ARM/MIPS 芯片跑满百兆带宽也仅占极低 CPU）
- **内容与摘要哈希**：`BLAKE3`（256 位 = 32 字节输出）

### 4.2 信任边界：内网织体 vs 对外发布面

每条网络连接在握手完成的瞬间，由 `colony-ident` 按对方公钥证书硬归入以下两个死隔离的半场，**不同半场在内核路由表上物理隔离**：

| 半场 | 谁在里面 | 准入凭证 | 内核允许它干什么 | 内核严禁它干什么 |
|---|---|---|---|---|
| **`Ring 0 · Self`**<br>（本我主权织体） | 同一个账号下的所有设备（主设备＋你的路由/音箱/Kindle/NAS/掌机） | 名录校验未吊销的 `DeviceCert`（v1 主设备名录/v2 服务目录） | 按证书角色与 K-7 授权线透明互调 `colony://` 能力、借用算力、同步 CRDT 切片、互传零拷贝/多路数据流 | 超出该设备 `DeviceCert.allowed_scopes` 的越权访问（如音箱证书无权拉取主库全量私密日记） |
| **`Ring 1 · Published`**<br>（对外发布面） | 一切外部世界——亲友联机、陌生节点、公海做种方 | 不需要认识对方：**外部连接只能打到你 work 里发布的节点**，可达范围被那根节点授权线圈死 | 只允许与发布节点本身交互：联机房的帧通道、共享屏的投屏口、**公共块交换节点**（按 `b3://<hash>` 换公开分块、逐块验哈希、换创作者签名的公开作品目录——它是默认发布的特例节点）。账本中的实现＝一条 `subject_id = "external:*"` 的普通 `CapabilityLine`，节点本体（agent 或代码）无任何特殊性 | **物理断绝与内网的任何路由**——外人连你家有几台设备、装了什么节点、有没有模型算力，一个字节都探测不到；节点被剪线瞬间，外部世界对你的可见性归零 |

> **旧"三环"收敛为两半场的理由**：亲友与陌生人不再需要内核级区别对待——他们都是"外人"，区别只在你的发布节点愿意让他们看见什么。要不要好友白名单是节点自己的业务逻辑，不是内核概念。外部连接永远不许路由进内网深处，是这半场唯一的硬规则。

### 4.3 核心身份数据结构（字段唯一锁死）

```rust
/// 账号根密钥（经主设备审批）签发给圈内每台设备的通行证（Ring 0）
pub struct DeviceCert {
    pub version: u8,                     // 固定 = 1
    pub account_pub: [u8; 32],           // 所属用户的账号公钥 (= Colony Account ID，v2 起绑定于官方账号服务名录)
    pub account_epoch: u64,              // 内部代次（吊销/恢复时 +1，旧代次凭证全作废——实现细节，用户不可见）
    pub device_id: [u8; 16],             // blake3(device_pub)[0..16]
    pub device_pub: [u8; 32],            // 该设备自身的 Ed25519 公钥
    pub device_name: String,             // 如 "bedroom-kindle", "living-speaker", "home-nas"
    pub role: DeviceRole,                // Primary | VaultNode | ComputeNode | EdgeOrgan
    pub allowed_scopes: Vec<String>,     // 允许同步的数据切片前缀与允许调用的能力族
    pub issued_at_ms: u64,
    pub expires_at_ms: u64,              // 支持定期自动续签；丢失不续自然失效
    pub root_signature: [u8; 64],        // 账号根密钥对字段确定性序列化的签名；吊销以名录为准（v1 主设备、v2 服务目录）
}

/// 每台入圈设备自动向 Ring 0 广播的「物理器官清单」（让老硬件秒变可连线节点）
pub struct OrganAdvertisement {
    pub device_id: [u8; 16],
    pub seq: u64,                        // 状态递增序号
    pub power_state: PowerStateSummary,  // AC | Battery(u8%)
    pub host_tier: IsolationTier,
    pub organs: Vec<OrganEndpoint>,      // 例: [{kind: "eink.screen", cap: "colony://d1/organ.eink/render", schema_hash: ...}]
    pub device_signature: [u8; 64],
}
```

### 4.4 「申请→审批」入圈协议（新设备是申请方，主设备是审批方——有无屏幕皆可）

配对的方向只有一个：**新设备申请，主设备审批**。二维码只是双方都带屏时的快捷方式，绝不是必需环节——NAS/路由/游戏机没摄像头照样入圈：

1. **申请**：新装了 `colonyd` 的设备上线即在局域网广播「我在申请入圈」（附 `device_pub` 与 `HostCaps`）；无屏设备可同时在自己的安装日志/CLI/管理页吐出一个 8 位申请短码（PAKE 防暴力猜解，5 分钟有效）；异地设备走寻址信标到达（v1 由主设备/社区节点兼任，v2 由官方服务提供）。
2. **审批**：主设备（带屏那台）弹出「待审批：客厅那台 NAS，指纹 abc…def」，用户确认即签发 `DeviceCert`；也可以反向——用户在主设备上输入新设备吐出的短码完成确认。
3. **落证**：`DeviceCert` 落回新设备 `$DATA_DIR`，发证方同步登记绑定关系并广播全圈（v1 主设备自持名录，v2 账号服务名录）——从此该设备断电重启自动重连，零人工干预。
4. **吊销**：把设备踢出圈＝名录标死（v1 主设备广播、v2 服务目录），各设备缓存名单下次连线即生效；离线期间按缓存名录干活（反正断网也加不了新设备）。

### 4.5 入圈 ≠ 接线：器官池与 work 级连线

设备入圈只是进了**可信器官池**——它持 `OrganAdvertisement` 声明能力后在池里休眠零内存躺着，什么都不干。**真正的节点图是 work 级的**：激活一个蓝图/work 时才实例化节点、连授权线——音箱可以拖进早报 work 当语音节点，也可以拖进安防 work 当监听节点，同一件器官进不同的活干不同的事。设备能力以标准器官节点端口暴露，work 的连线机制原样适用，一根线都不多发明。器官插件层优先**包装社区现成集成**（Home Assistant 生态已覆盖几千种硬件接入），不从零造轮子。

---

## §五、K-3 / K-4 统一内容寻址底座（`b3://` CAS）、A/B 生命线与增量海关

### 5.1 为什么内核内置极简 `b3://` 分块仓是点睛之笔

全系统所有不可变大对象——**系统增量包（`.cext`）、AI 模型权重（`.gguf` / `.onnx`）、去中心化分发的 50GB 独立游戏（`.capp`）及共享兼容层（`Δ-Proton`）**——底层存储格式完全统一：
- **分块规格**：按固定 **`262,144` 字节（256 KiB）** 切块，每块算 `BLAKE3` 哈希，拼成标准 Merkle 树，树根 32 字节哈希即为全球唯一内容地址 `b3://<64位hex>`。
- **三大全景收益**：
  1. **边下边玩（流式挂载）**：第二阶段掌机从家里 NAS 或公网 P2P 打开一个 30GB 独立游戏时，不需要等 30GB 下完——内核先拿几 KB 的 Merkle 树头，游戏读到哪一块就优先拉哪一个 256KB 块，当场验过 `BLAKE3` 立即喂给游戏进程，篡改块零容忍；
  2. **跨应用/跨增量零冗余去重**：10 款独立游戏都依赖同一个引擎运行时块，`$CAS_DIR` 物理磁盘上只存一份；
  3. **极小代码体积**：`colony-cas` 整个模块不到 600 行纯 Rust，路由器与 Kindle 同样跑得飞起。
- **CAS 与主权库的边界（铁规）**：块仓只装**不可变分发对象**——`.cext` 增量、模型权重、`.capp` 作品本体、兼容层。**一切用户可变状态（画像、存档、批注、资产）永远进 L1 主权库走 CRDT，不进块仓、不进内核账本**——内核机制里没有"存档"这个概念，它只认哈希块。

### 5.2 K-3 双槽启动与自动回退有限状态机（FSM 唯一锁死）

存储在 `boot/slot_state.bin`（带 CRC32 双备份防撕裂写）中的槽位状态机只有 **4 个合法状态**，严禁自创中间态：

| 当前状态 | 触发事件 | 下一状态 | 内核/引导器执行的确定性动作 |
|---|---|---|---|
| `SLOT_HEALTHY` | 发起系统升级写入另一空闲槽 `alt_slot` 并验过 `b3://` 与根签名 | `SLOT_TRY_BOOT (tries_left=3)` | 将下次启动目标指向 `alt_slot`，原健康槽保留为 `fallback_slot` 绝不擦写 |
| `SLOT_TRY_BOOT (tries_left > 0)` | 机器上电/重启引导 | `SLOT_PENDING_COMMIT (tries_left - 1)` | 原子递减 `tries_left` 落盘，启动硬件/内核看门狗（默认 90 秒），拉起新槽 `colonyd` |
| `SLOT_PENDING_COMMIT` | 新 `colonyd` 完成账本打开、沙箱自检、IPC 就绪，主动调 `boot.commit_healthy()` | `SLOT_HEALTHY` | 清零回退计数，停看门狗升级期倒计时，本次升级正式转正 |
| `SLOT_PENDING_COMMIT` 或 `SLOT_TRY_BOOT (tries_left == 0)` | 看门狗超时、内核 panic、断电重启导致 `tries_left` 耗尽仍未 Commit | `SLOT_ROLLED_BACK` → 切回 `fallback_slot (SLOT_HEALTHY)` | 引导器直接启动原健康旧槽，并在 K-7 审计账记入一条 `boot_auto_rollback` 故障证据 |

### 5.3 K-4 增量包（`.cext`）清单契约 `ExtensionManifest`

```rust
pub struct ExtensionManifest {
    pub format_version: u8,              // 固定 = 1
    pub ext_id: String,                  // 如 "colony.delta.engine", "colony.adapter.kindle_eink"
    pub semver: [u16; 3],                // [major, minor, patch]
    pub abi_min_version: u16,            // 要求的最低内核契约锚版本
    pub root_cas_hash: [u8; 32],         // 镜像根文件系统在 CAS 中的 BLAKE3 根哈希
    pub provides_contracts: Vec<String>, // 向 K-5 注册提供的契约名，如 ["model.infer/v1", "model.embed/v1"]
    pub requires_contracts: Vec<String>, // 依赖的其他契约名
    pub required_host_caps: Vec<String>, // 如 ["alsa_audio"]，不满足则 K-4 具名拒装 E_HOST_CAP_MISSING
    pub wake_triggers: Vec<WakeTrigger>, // 声明休眠唤醒条件（见 §六），支持平时 0 内存常驻！
    pub resource_budget: QuotaSpec,      // 声明自身沙箱所需的内存上限、CPU 权重与执行等级
    pub publisher_pub: [u8; 32],
    pub signature: [u8; 64],
}
```

### 5.4 三包裹进门信任链（本地信任根，无平台审查员）

| 包裹 | 是什么 | 进门卡（内核硬查） | 审查形态 | 落地 |
|---|---|---|---|---|
| `.cext` 系统增量 | 系统级提供者：驱动器官、注册新契约（`provides_contracts`）——**全系统最深的能力面，唯一需最严卡的包裹** | 创作者签名＋CAS 验哈希＋**契约清单单独弹窗确认**（"要注册 `model.infer`、`organ.x` 两个新能力，认吗"）；陌生签名加亮警示 | 契约清单确认＝审查 | 注册后只是普通居民，授权线＋沙箱照样管死 |
| `.capp` 作品（两亚型） | ① **密封应用**：整体世界（独立游戏、车机功能），传统打包形态；② **蓝图作品**：开放节点图，按内容寻址引用若干 `.cnode`——图可改、节点可换 | 创作者签名＋验哈希；蓝图亚型额外要求**每个被引用节点各自带有效签名** | 密封包：亮明签名者；蓝图：**审查＝激活前亲眼看接线图**（图是人话可读的，图即审查对象） | 沙箱运行，能干什么＝你给的接线图 |
| `.cnode` 节点 | 可加入任一蓝图的单元（agent 或代码） | 独立签名＋验哈希 | 亮明签名者；能力边界全由所属 work 的线图决定 | 节点沙箱，越线即 `E_LINE_DENIED` |

- **信任根在用户本地**：出厂只带官方发布签名根；社区信任根（某审查组/某大佬的公钥）由用户自己钉（`known_hosts` 式）——陌生自签可以装，但签名者身份亮明、风险自担；
- **没有"平台审查员"这个角色**：信任链拆成三层各管各的——**完整性**（签名＋哈希，内核硬卡）、**出处可见**（谁签的亮给你）、**能力边界**（接线图＋沙箱，越不过）。审查被分散成了"签名可追溯＋用户亲眼看图＋自选社区信任根"；
- **蓝图开放**：`.capp` 蓝图按内容寻址引用节点，换节点/加节点＝改接线图＝用户重签生效——社区可以只贡献一颗好节点，不必打包整个作品。

---

## §六、K-5 双平面契约锚总线：24 字节线帧 ＋ 零拷贝/多路 QoS 数据面

这是全系统最关键的「窄腰」。不论居民是用 Rust、Go（如未来的 CardColony）、C++（游戏引擎）还是 Python 写的，只要能读写 Unix Socket 并发出下面这个 **24 字节帧头**，就能无缝接入 Colony 全景宇宙。

### 6.1 控制面二进制帧头（`FrameHeader` —— 24 字节大端网络序，字节级锁死）

```
 0                   1                   2                   3
 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|      Magic = 0x43 0x4C 0x4E 0x59 ("CLNY")                     |  Bytes 0..3
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|  ProtoVer (1) |   FrameType   |   QoSClass    |     Flags     |  Bytes 4..7
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|                         StreamID (u64 BE)                     |  Bytes 8..15
|                                                               |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|                        PayloadLen (u32 BE)                    |  Bytes 16..19
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|                      LineEpochHint (u32 BE)                   |  Bytes 20..23
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|              Payload (MsgPack 编码，长度 = PayloadLen，上限 1 MiB) ...
```

- **`FrameType` 唯一合法枚举（1 字节）**：
  - `0x01 = CALL_REQ`（发起能力调用，载荷含 `target_uri`, `method`, `idem_key`, `args`）
  - `0x02 = CALL_RESP`（调用回执，载荷含 `status_code`, `result_or_err`，若升级为零拷贝通道则附带 `SCM_RIGHTS` FD！）
  - `0x03 = STREAM_OPEN` / `0x04 = STREAM_DATA` / `0x05 = STREAM_CLOSE`（流式对话 Token、音频帧或跨机分块流）
  - `0x06 = EVENT_WATCH` / `0x07 = EVENT_DELIVER`（注册唤醒条件与投递唤醒事件）
  - `0x08 = LEASE_CTL`（申请子沙箱租约、查询水位、`tree.kill` 强杀）
  - `0x09 = PING` / `0x0A = PONG`
  - `0x0F = FENCE_ABORT`（内核代次围栏跳变、授权线被剪断时的立即中止帧）
- **`QoSClass` 四级优先级（1 字节，贯穿本地队列与跨机加密隧道）**：
  - `0 = RT_Critical`（紧急停止令、车机控制、独占音频/手柄输入帧 —— 绝对最高优先，永不排队阻塞）
  - `1 = Interactive`（用户主动开口语音、前台点击触发的实时推理、**当前运行中原生 AI 游戏的实时对话/声线/导演请求**、正面面板读缓存）
  - `2 = Batch_Compute`（后台多智能体并行任务、**原生 AI 游戏世界内屏幕外数十个 NPC 的群体波次推演**、小模型波次攒批过滤、向量计算）
  - `3 = Opportunistic_Swarm`（冷数据画像凝炼、全网 P2P 游戏后台预载与做种 —— 前三级有流量时立即让出带宽与算力）
- **`Flags` 关键控制位（1 字节位掩码，精确定义）**：
  - `bit 0 (0x01) = HAS_FD`：本帧附带 `SCM_RIGHTS` 零拷贝文件描述符（如共享内存 `memfd`、音频管道、只读 LoRA 权重块映射）
  - `bit 1 (0x02) = SEAT_AFFILIATED`：**前台独占席位亲和标记（专为原生 AI 游戏打造！）**——由内核 `colony-bus` 自动盖章（调用方严禁伪造，仅当前持有 `10_rt_seat` 的前台游戏进程发起的调用由内核置 1），用于在全屏游戏态下精准放行「属于这款游戏自己的 AI 大脑与 NPC 群体推演」，同时冻结其他无关后台杂活！
  - `bit 2 (0x04) = PREFER_OFFLOAD`：本地渲染繁忙提示位——若圈内有空闲的插电算力节点（如书房 PC / NAS），总线优先透明路由至远端显卡执行，保本机掌机 100% 显存与帧率。
- **单帧硬上限**：控制面 `PayloadLen > 1,048,576 (1 MiB)` 时，`colony-wire` 不分配内存直接断开并报 `E_FRAME_OVERSIZE`——**从协议根上逼迫一切大块数据必须走下面的零拷贝/流式数据面！**

### 6.2 数据面双快路（为什么 120FPS 游戏和 50GB 传输在 Colony 上零折损）

当调用方发起大吞吐交互（如游戏挂载 CAS 块、相机/音频实时流、本地模型共享大段上下文或输出图像到墨水屏）且通过 `K-7` 授权线核验后：
1. **同机零拷贝快路（Local Zero-Copy Path）**：
   `colonyd` 创建一对内核管道/`memfd` 密封共享内存（或取得硬件 `drm/dmabuf/alsa` 受限设备描述符），在 `CALL_RESP (Flags |= HAS_FD)` 中通过 Unix Socket `SCM_RIGHTS` 直接把描述符塞给调用方与服务方进程。**此后数 GB 数据在两进程间内核态直通，`colonyd` 占用 0% CPU！**
2. **跨机织体多路流快路（Remote Fabric Stream Path）**：
   若目标地址 `colony://<remote_dev>/...` 在另一台设备上，调用方本地代码**完全不用改**——`colonyd` 在本机给调用方一端管道 FD，另一端在内核态直接泵入 `colony-ident` 加密隧道的对应 `QoSClass` 子流传往远端设备。**对节点开发者而言，调本机和调十公里外的家里电脑，API 100% 同构。**

### 6.3 `colony://` 寻址语法与 `cap_pat` 通配规则（三处共用同一套语法）

调用帧 `target_uri`、授权线 `target_device_pat`/`target_cap_pat`、器官广告 `OrganEndpoint.cap` 三处**必须使用同一套地址语法**，严禁各方言：

```
colony://<设备域>/<契约>/<方法路径>

设备域：  self            → 仅本机
          <device_id>     → 指定设备（16 字节 hex）
          ring0:*         → 织体内任一够格设备（调度器择优路由，调本机与调异地同构）

契约：    能力族标识，如 organ.eink | model.infer | model.embed |
          profile.read | net.relay | cas.read | power.seat ...
          （契约名由 .cext 增量经 provides_contracts 注册，严禁私造；
            内核无任何内置模型或官方默认模型——model.* 契约类型开放生长）

方法路径： / 分隔的层级名，如 render | slot:fast | slice:health
```

**通配规则（仅用于授权线 pattern，调用帧必须写完整地址）**：`*` 只允许作为**末尾整段**出现，匹配该段及其下所有层级——合法：`organ.eink/*`（该契约全部方法）、`model.infer/slot:*`（全部槽位）、`ring0:*`（织体任意设备）；非法：`organ.*/render`（中段通配）、`mo*`（前缀通配）。

### 6.4 休眠零占用唤醒状态机（Dormant Activation FSM）

为让百元路由器、Kindle 与普通 PC 同时挂上百个「活节点」而不吃光内存，居民默认遵循五态循环：
1. `DORMANT`（进程未运行，RAM = **0 字节**，唤醒触发器静静躺在 K-5 内存索引中，每条仅占 ~128 字节）
2. 收到匹配事件（定时到点 / 信道来信 / 硬件按键 / 数据表摘要变更）→ 转 `WAKING`（经 `colony-sandbox` 按其配额拉起进程）
3. 进程连上 `anchor.sock` 领走积压事件批 → 转 `ACTIVE`
4. 事件处理完且空闲超过 `idle_linger_ms`（轻量设备默认 **500ms**，PC 默认 **5000ms** 防频繁冷启抖动）→ 发排空信号转 `DRAINING`
5. 进程干净退出（超时未退由沙箱收割）→ 回 `DORMANT`（RAM 归零）。

---

## §七、K-6 沙箱围栏、硬件席位直通与电源/实时负载调速闸

### 7.1 根 `cgroup v2` 五宫格硬分区（防多智能体与大模型跑死整机）

在支持 `Tier1_Full` 的一切机器上，`colony-sandbox` 开机即建立固定五宫格，任何一行代码不得在宫格外裸起进程：

```
/sys/fs/cgroup/colony/
├── 00_system_reserve/   # colonyd 本体 + 紧急控制通道：memory.min = max(32MB, 5% RAM)，cpu.weight = 1000（铁保底，永不可挤占）
├── 10_rt_seat/          # 当前获批独占屏幕/手柄的前台游戏 (.capp) 或车机导航：高优先级 CPU + GPU/DRM 直通句柄
├── 20_interactive/      # 超级交互入口 UI + 实时语音/指令节点
├── 30_batch_compute/    # Δ-调度发动机的本地模型实例池 + CardColony 派生的并行编译/工具子沙箱树（统一封顶 memory.max <= 75% RAM）
└── 40_opportunistic/    # P2P 做种传输、冷画像凝炼、后台索引同步（空闲跑；紧急时一键 cgroup.freeze = 1 瞬间冰封！）
```

- **推理池 ↔ 沙箱池 ↔ 游戏显存池三方硬联防（破解掌机/主机 APU 统一内存爆显存顽疾）**：
  - 掌机、迷你主机与 SoC 普遍是 `has_unified_vram == true`（16GB/24GB 物理内存由游戏贴图渲染与本地大模型共抢）。`colony-sandbox` 在批出 `10_rt_seat` 游戏独占席位时，先锁死该游戏声明的渲染显存保底额度 `seat_mem_reserve`，动态下调 `30_batch_compute` 的 `memory.max` 天花板（且若有 `has_npu_accel` 则优先引导小模型走独立 NPU），从内核物理层面杜绝「AI 推理把正在打的游戏挤崩闪退」。
  - 当并发请求使 `30_batch_compute` 逼近动态天花板时，内核绝不触发 OOM Killer，而是确定性返回 `E_QUEUED (1)` 或自动走 `PREFER_OFFLOAD` 甩给局域网同伴主机。

### 7.2 电源与独占游戏负载自动调速表（Power & Seat Governor —— 精准区分「游戏自己的 AI」与「无关杂活」）

内核状态改变时，无需上层配合，K-6 结合帧头上的 `SEAT_AFFILIATED` 内核印章自动执行硬件级分流调控：

| 机器当前态势 | 自动对 `40_opportunistic` 做的事 | 自动对 `30_batch_compute` 做的事 | 解决的真实痛点 |
|---|---|---|---|
| **插电常规态**（NAS/路由/插电 PC） | 全速放行 | 满额放行波次攒批 | 半夜家里 NAS 满血替主人做种、下游戏、算画像 |
| **独占原生 AI 游戏激活**（掌机/主机正跑 `.capp` 游戏） | **立即 `cgroup.freeze = 1` 冰封无关做种与冷任务（0% CPU）** | **一分为二精准执法**：① 无关后台蓝图任务立即暂停入队；② **带 `SEAT_AFFILIATED` 印章的「该游戏自身的活体 NPC / 语音 / 世界推演」专享帧安全算力配额**（在家连网时更自动开启 `PREFER_OFFLOAD` 借家里 PC 显卡跑重模型，离线时走本机 NPU/受控显存槽） | **既保证 120 帧丝滑不掉半帧，又让未来原生 AI 游戏里的整个智能体世界火力全开！** |
| **电池供电常规态**（掌机看面板 / Kindle 阅读） | 限制唤醒频率为每小时最多 1 次短同步 | 仅响应主动请求，禁止后台大批量重算 | 移动设备续航不尿崩 |
| **低电量告警（Battery < 15%） / 深度休眠合盖** | **立即冰封** | **拒收本机新批次，自动透明委托圈内插电主机执行** | Kindle 待机几周不掉电；掌机低电时照样能借家里算力玩 AI 对话游戏 |

---

## §八、K-7 最小系统账本、能力缩权线与代次围栏（Epoch Fence）

### 8.1 五张内核铁账（单文件嵌入式事务表，全部 Key-Value 布局锁死）

`colony-ledger` 只维护以下 5 个命名空间表（Key 与 Value 均为确定性结构体编码）：
1. `tbl_meta`：`account_pub`、`account_epoch`、本机 `device_id`、全局单调递增 `global_line_epoch: u64`；
2. `tbl_devices`：`device_id -> DeviceCert`（圈内已知同伴及吊销黑名单 `tbl_revoked_devices`）；
3. `tbl_residents`：`resident_id -> ResidentRecord`（已装 `.cext` 增量、激活蓝图节点、`.capp` 应用的清单摘要与 CAS 根哈希）；
4. `tbl_lines`：`line_id -> CapabilityLine`（**全系统唯一权限权威真相**：谁被用户允许连谁）；
5. `tbl_audit_ring`：`seq_u64 -> AuditEntry`（定长环形缓冲，路由默认留最近 2,000 条，PC 留 100,000 条；专记越权拒绝、授权变更、回退故障、设备入退圈，满环覆盖最老普通记录但永久保留未确认安全告警）。

### 8.2 授权线结构体 `CapabilityLine` 与「只减不增」派生铁律

```rust
pub struct CapabilityLine {
    pub line_id: [u8; 16],
    pub parent_line_id: Option<[u8; 16]>, // 若为父智能体派生给子沙箱的临时线，指向父线
    pub line_epoch: u32,                  // 创建/修改该线时的代次号
    pub subject_id: String,               // 调用方身份，如 "blueprint:morning_paper/node:summarizer"
    pub target_device_pat: String,        // 允许的目标设备，"self" | "ring0:*" | 指定 device_id
    pub target_cap_pat: String,           // 允许的能力模式，如 "organ.eink/render" 或 "model.infer/slot:fast"
    pub constraints: LineConstraints,     // 速率限制、单日算力配额、只读/读写标记、允许的数据切片前缀
    pub state: LineState,                 // Active | Revoked
    pub granted_by_user_sig: [u8; 64],    // 顶层线必带用户主权/本机管理授权签名；子派生线必满足 constraints ⊆ 父线！
}
```

- **连线即授权（内核不存在"权限授予"协议）**：顶层 `CapabilityLine` 只由用户签名的接线动作产生——激活一张蓝图/修改接线图/手动连线，落成账本就是它。内核没有"运行时请求权限"API：节点调用未连线的能力只会拿到 `E_LINE_DENIED`；要新线＝上层把"建议加线"呈现给用户重签接线图，内核永远只见一种动作（用户签名落线）。
- **只减不增铁律（Attenuation Invariant）**：当 CardColony 或某个蓝图父节点为子任务申请派生临时 `CapabilityLine` 时，`colony-bus` 机械核对 `child.target ⊆ parent.target && child.constraints ⊆ parent.constraints`——子任务永远不可能凭空放大父节点没有的权限。
- **代次围栏瞬时断权（Epoch Fence）**：用户一旦在界面剪断某根线（或吊销某台设备），在一个原子事务内：① `line.state = Revoked` 且 `global_line_epoch += 1`；② `colony-bus` 扫描活跃流表，凡绑定在该 `line_id`（及其所有子孙 `parent_line_id`）上的活跃连接立即发出 `0x0F FENCE_ABORT` 并关闭 Socket、销毁关联 `memfd` 映射；③ 通知 `colony-sandbox` 对该线派生的在途临时子租约执行 `tree.kill`。**撤权延迟硬指标：< 10 毫秒，零在途偷跑。**

---

## §九、全域统一状态与错误码注册表（严禁各模块自造错误码）

所有控制面 `CALL_RESP`、内部 Rust `Result<T, KernelError>` 及跨语言 SDK 必须严格使用以下唯一编号表：

| 码值 (`u16`) | 常量名 | 分类与调用方必须遵守的处理规矩 |
|---|---|---|
| `0` | `E_OK` | 成功完成，读取返回载荷或附带的零拷贝 FD |
| `1` | `E_QUEUED` | **合法延后（算力攒批/离线缓存投递基石）**：请求已被内核/调度器安全入队并附带 `ticket_id`，**调用方严禁当失败立即重试**，应等待事件回调或流式结果 |
| `2` | `E_BACKPRESSURE` | 队列与沙箱水位双满（显式背压）：调用方必须退避等待资源释放信号，不得死循环硬撞 |
| `10` | `E_LINE_DENIED` | 无匹配的 `Active` 授权线（越权访问被 K-5/K-7 拦下，已记入审计环） |
| `11` | `E_EPOCH_STALE` | 通道或票据绑定的 `line_epoch` / `account_epoch` 已过期（刚发生撤权或主密钥轮换） |
| `12` | `E_CERT_INVALID` | 设备证书签名错误、已过期或设备已在吊销黑名单中 |
| `13` | `E_RING_VIOLATION` | 跨信任边界越界（如公网节点企图发控制帧访问内部总线） |
| `14` | `E_ATTENUATION_VIOLATION` | 派生子线试图申请超出父线范围的权限 |
| `20` | `E_CAP_UNAVAILABLE` | 当前本机及圈内在线设备均无提供该契约的活跃增量（调用方应走优雅降级，如无 GPU 时退纯文本） |
| `21` | `E_HOST_CAP_MISSING` | 当前硬件不满足增量要求的底层特性（如在无声卡路由上装音频器官） |
| `22` | `E_POWER_THROTTLED` | 设备正处于低电保护或独占游戏/实时席位态，后台低优请求被调速闸暂拒 |
| `30` | `E_HASH_MISMATCH` | `b3://` CAS 块校验或包签名不匹配（发现损坏或投毒，立即丢弃该块并拉黑来源节点） |
| `31` | `E_ABI_INCOMPATIBLE` | 增量要求的 `abi_min_version` 高于当前内核版本 |
| `40` | `E_QUOTA_EXCEEDED` | 超出该居民声明的内存/磁盘/算力配额上限 |
| `50` | `E_FRAME_MALFORMED` | 魔数不对、序列化损坏或未知帧类型（立即断开该连接） |
| `51` | `E_FRAME_OVERSIZE` | 控制面单帧超过 1 MiB 红线（必须改走零拷贝/流式数据面） |

---

## §十、给未来自动开发智能体（DeepSeek 等）的十条「防呆死律」

将来无论把这份方案拆成多少张施工卡交给重构后的 CardColony 全自动执行，每张写内核与系统增量的智能体卡都必须通过以下十条机械自检，缺一条不许交卡：

1. **零猜测线形**：凡跨进程/跨设备通信，只许调 `colony-wire` 的 24 字节 `"CLNY"` 帧头，严禁私开裸 HTTP/JSON 端口绕过契约锚。
2. **零硬编码机型**：代码里搜出 `"kindle"`、`"openwrt"`、`"steamdeck"` 字符串作分支判断的直接判红——一律只许读 `HostCaps` 能力位。
3. **零 C 语言编译泥潭**：`colonyd` 核心依赖树严禁出现 `openssl-sys`、`libdbus-sys`、`systemd` 等宿主 C 库绑定，确保一条 `cargo build --target armv7-unknown-linux-musleabihf` 100% 一次静态编过。
4. **大块必走 CAS 与快路**：大于 1 MiB 的数据传输必须走 `SCM_RIGHTS` 共享描述符或 `STREAM_DATA` 分块流，所有持久只读包必须经 `colony-cas` 验过 `BLAKE3`。
5. **发布面铁幕**：对外发布节点收到超出其授权线范围的任何请求（如公网 P2P 端口收到非 CAS 块交换/公开目录同步的请求），第一行直接回 `E_RING_VIOLATION` 断连。
6. **`E_QUEUED` 不是报错**：所有客户端 SDK 与节点模板必须把 `status_code == 1 (E_QUEUED)` 作为正常异步挂起态处理，严禁触发重试风暴。
7. **撤权必测在途清场**：测授权线不能只测「没线调不通」，必须测「正连着流传数据时剪断线，10ms 内流被掐断且子沙箱进程归零」。
8. **回退必测真强杀**：测 A/B 启动槽与账本原子性，必须在独立子进程中用真 `SIGKILL` 随机强杀验证恢复，严禁用内存假替身冒充落盘通过。
9. **保底池神圣不可侵犯**：压测卡把 `30_batch_compute` 故意塞满触发背压时，`00_system_reserve` 内的 `PING/PONG` 与 `tree.kill` 响应延迟必须依然 < 10ms。
10. **单模块行数封顶**：每个内核子 Crate 生产代码超过 1,500 行即触发拆分预警，保持 Flash 级模型单次全量通读零遗漏。

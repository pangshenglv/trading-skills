# trading-skills

个人交易工具与 Skills 仓库（与 QQQ 0DTE 自动交易系统仓库分开维护 ✓）。

## 内容

| 路径 | 说明 |
| --- | --- |
| `skills/weekly-option-screen/SKILL.md` | 周度期权筛选：口径 + 方法论 + **IB Gateway 取数硬规则** |
| `skills/weekly-option-screen/screen.py` | 可直接运行的筛选脚本（分批取数/配额保护/σ 反解/自检/优雅降级） |
| `examples/` | 历史运行结果样例 |

## 用法

```bash
python3 skills/weekly-option-screen/screen.py --out 结果.md
# 参数：--expiry YYYYMMDD（默认本周五）/ --min 15 --max 40 / --sides both|calls|puts / --names A,B,C
```

## 关键口径（用户设定 ✓）

- 预算 **$15–40 / 张**（$25 为主力档）
- 到期 **默认本周五**，可指定
- 方向 **call + put 都出**
- 玩法 **买权利金波段**：赚了就跑，不等变实值、不等到期

## 依赖与连接

- `ib_async`；IB Gateway `127.0.0.1:7497`
- **独立 clientId（26/27/28）** ✓ —— **绝不使用 42**（QQQ 实盘系统占用 ✗）
- **同一时刻 ≤20 张合约**，取完立即取消（超过会被 IBKR 静默限流并卡死 ✗）

## 免责

以上为数据分析，非投资建议 ✓；期权可全损 ✓。

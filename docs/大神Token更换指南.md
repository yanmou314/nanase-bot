# 大神 Token 更换指南

> 适用服务：overstats（守望先锋数据 API）
> 什么时候需要换：机器人查询大神数据失败，`journalctl -u overstats` 里出现 401/403 鉴权错误
> 官方文档参考：overstats 项目的 `Faststart.md`（本地 /opt/overstats/Faststart.md）

## 快速流程（2 步）

### 第 1 步：F12 申请新 token

1. 电脑 Chrome 打开 https://ds.163.com/ 并登录
2. 按 F12 → Console（控制台）
3. 粘贴下面的脚本，回车（role_id 已填好，不用改）：

```js
(async () => {
  const url = "https://inf.ds.163.com/v1/web/game/report/getReportToken";
  const payload = { appKey: "bn", roleId: "236537768", server: "1", source: 1, type: "yearly" };

  const body = JSON.stringify(payload);
  // 调用页面自身的签名模块生成请求签名
  const sigMod = await window.sig.default();
  const signObj = JSON.parse(sigMod.gen_sign(body));

  const getCookie = (name) =>
    document.cookie.split("; ").find(r => r.startsWith(name + "="))?.split("=").slice(1).join("=") || "";

  const resp = await fetch(url, {
    method: "POST",
    credentials: "include",
    headers: {
      "Content-Type": "application/json;charset=UTF-8",
      "GL-ClientType": "61",
      "GL-DeviceId": localStorage.getItem("ns-client-id") || localStorage.getItem("ds-website-uuid") || "",
      "GL-Uid": getCookie("GOD_UUID"),
      "GL-X-XSRF-TOKEN": getCookie("GL-XSRF-TOKEN"),
      "GL-CheckSum": signObj.sign,
      "GL-Nonce": String(signObj.timestamp),
    },
    body,
  });

  const json = await resp.json().catch(() => null);
  console.log("status =", resp.status);
  if (json?.result?.token) {
    console.log("%c✅ token =", "color:green;font-size:14px", json.result.token);
    console.log("%c✅ role_id =", "color:green;font-size:14px", json.result.roleId || "236537768");
  } else {
    console.log("原始返回 =", json);
  }
})();
```

4. 控制台绿色打印 `token = ...`，复制那串值

### 第 2 步：写入服务器

SSH 上服务器后执行（把"新token"换成刚拿到的值）：

```bash
mkdir -p /etc/systemd/system/overstats.service.d
cat > /etc/systemd/system/overstats.service.d/dashen-token.conf <<EOF
[Service]
Environment=DASHEN_TOKEN=新token
EOF
systemctl daemon-reload && systemctl restart overstats
```

> 如果连 role_id 也换了（换绑了别的战网账号），在 EOF 前加一行：
> `Environment=DASHEN_ROLE_ID=新role_id`

## 第 3 步：验证

```bash
systemctl is-active overstats
journalctl -u overstats --since "2 min ago" | grep -iE "401|403|auth" | head -5
```

- 服务 active 且无鉴权报错 = 成功
- 再在群里让机器人实际查一次大神数据最保险

## 第 4 步：让旧 token 失效（推荐）

配置成功后，去 ds.163.com 退出登录再重新登录一次，
旧会话和旧 token 即失效，泄露过的旧 token 就彻底没用了。

## 常用排查命令

```bash
# 看当前配置的 token（中间打码）
sed -E "s/(DASHEN_TOKEN=.{4})[^=]*(.{4})/\1********\2/" /etc/systemd/system/overstats.service.d/dashen-token.conf

# 看服务内存状态
systemctl show overstats -p MemoryCurrent --value

# 看最近的上游请求是否正常
journalctl -u overstats --since "10 min ago" | tail -20
```

## 注意事项（重要）

- **token 是按年度签发的**（接口参数 type=yearly）：同一账号同一周期内重复跑脚本，返回的都是同一枚 token，重跑不等于换新
- **想强制换新**：先在大神 App + 网页版全部退出登录（必要时改网易密码），重新登录后再跑脚本；若返回值与当前不同才需要更新配置
- 年度周期切换（如 2026→2027）后 token 可能变化：服务出现 401/403 时，跑第 1 步脚本拿最新值，按第 2 步更新
- 泄露影响评估：该 token 为只读查询凭证，泄露后果仅是他人冒用查询配额，风险较低
- token 与 role_id 必须来自同一个大神账号（成对校验）
- 不要把 token 提交进 git 或贴到公开场合
- 如果脚本运行报 `window.sig` 相关错误：确认是在 ds.163.com 主页的控制台运行，且已登录

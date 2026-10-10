# YouTube 下载画质修复记录

日期：2026-10-11。状态：实现、测试、check 复查及文档同步完成；用户已授权最终检查通过后提交推送，实际提交状态以 Git 记录为准。

## 根因与修复边界

视频 yUuFvL1lK4k 在当前环境的 Chrome Cookie 提取仅暴露格式 18（640×360），匿名提取可获得 399+251-19（1920×1080）。带 Cookie 的 web/web_embedded 客户端报告缺少格式 URL，诊断指向 SABR；Deno JS 正常，本机配置有无均得到相同对照结果。原实现不比较匿名方式，接受低清结果且隐藏警告，因而把低清媒体登记为有效 source_video。旧 Job 未保存完整历史响应，本次证明当前可重复机制，不断言所有历史低清均由同一原因引起。

本次不新增命令、环境变量、依赖或自动凭据访问，不安装 PO Token/SABR 插件。范围限于显式传入 Cookie 的 YouTube 完整视频下载；无 Cookie、非 YouTube 与纯音频不进入双路径比较。格式选择使用 bv*+ba/b，显式 lang,res 排序，无 1080p 上限。

## 最终行为

1. 串行查询配置与匿名访问。匿名忽略 yt-dlp 本机配置、保留显式代理；两次查询均超时 60 秒。
2. yt-dlp 在单次提取内排序原音轨及分辨率。跨访问方式优先 language_preference=10 标记的原音轨，再比较短边分辨率；同等条件保留配置方式。两者均确认原音轨但已知基础语言不一致时，拒绝匿名替换。未知音轨身份明确记录 original_audio=false，不用字幕语言选择音轨。
3. 校验视频 ID、请求 URL ID、尺寸、静态视频与时长，两种结果时长差不超过 500ms。WARNING/ERROR 中的缺格式或提取失败信号参与完整性判断；健康结果里的 DEBUG web 客户端缺 URL 信息不直接代表整次提取不完整。获选路线或匿名路线不完整时拒绝；Cookie 路线不完整但完整匿名结果胜出时可恢复。
4. 固定选中格式 ID，不在下载失败后改选低清。下载位于输出目录内独立临时子目录，ffprobe 校验实际尺寸、预期音视频流及不超过 500ms 的时长差。
5. 以 hard link 原子发布，目标已存在、为 symlink 或并发出现时拒绝覆盖；失败只清理本次临时目录。摘要发布失败不使已验证媒体失效。
6. 两条路径完整且都只有 360p 时允许下载，摘要记录实际尺寸；当前普通 CLI 尚无专门的低清提示。匿名查询错误保守停止，受限内容可使用本地媒体入口。
7. 旧 checksum 有效媒体仍被复用，status 不访问网络。已有低清 Job 使用 burn --media 显式重烧，不更换源 cue、译文或时间轴。

## 诊断与恢复

成功结果附带可选 source.download.json：version、video_id、access、format_id、实际 width/height/duration_ms、audio_language、original_audio、reason 和 media_checksum。原因包括 anonymous_higher_resolution、anonymous_original_audio、configured_best。原始 JSON、Cookie、签名 URL 与自由格式提取日志不落盘。

pipeline 的转写与远程烧录入口登记 media_download_selection artifact；同名事件提供公共 Job 字段与 summary_path。摘要缺失、坏 JSON、非对象或媒体 checksum 不匹配时跳过。该诊断 artifact 是 downloaded_media/source_video 的失效下游，媒体不依赖摘要，不会因摘要丢失自动重下载。

## 验证与 check

- 新回归在原 download.py 上执行，Cookie 360p/匿名 1080p 用例失败（实际未发生两次比较）；修复后通过。
- 回归覆盖 1080p、4K/8K、同等条件保留配置、原音轨优先、未选中的无音轨候选、完整低清、视频 ID/时长/语言不一致、缺格式警告、匿名失败、实际尺寸错误、目标存在与并发创建、下载失败、无效 JSON、摘要容错及失效依赖方向。
- 共享工作区：868 tests、176 subtests 通过；Ruff lint、formatter、xvt --help、xvt doctor --json 通过。
- 仅含本次修改的隔离目录：867 tests、169 subtests 通过，另有两个既存布局 QA 子测试失败。已换回 HEAD 的 burnin.py 复现相同失败，触发于 test_burn_accepts_acknowledged_needs_review_qa 对旧持久化布局 QA 状态的断言；共享工作区此前已有的布局修复使其通过。本次未借此修改或提交无关 QA 代码。
- 最终提交范围包含当前分支已有的布局 QA 恢复修复。对全部 21 个受审文件重新进行隔离验证：868 tests、176 subtests 全部通过，Ruff lint 与 formatter 通过；此前下载修复独立验证的基线失败已由这部分布局修复解决。
- check 的架构、安全、四角度对抗复查均完成。发现并修复匿名不完整候选、坏摘要容错、未选中的无音轨候选误拒绝与发布覆盖竞态；最新复查均无剩余可操作发现。
- 获得用户授权后，实际整段下载到临时目录，结果 1920×1080、AV1 + Opus、时长 200634ms，选中英文原音轨 en-US。查询时长 201000ms，差值 366ms 在允许范围内。源 Job 的源文、译文及 SRT checksum 保持不变。未重跑翻译或烧录。

## 交付与限制

实现和诊断登记按不超过六个文件的编辑组完成，文档分组同步。当前功能分支同时包含原有布局 QA 恢复修复；最终交付检查覆盖两部分修改，未切换分支。用户已授权检查通过后提交推送，不自动合并 main 或发布版本。

最高质量指当前两种受支持访问方式暴露且可下载的原音轨视频，不承诺取得平台未暴露或需额外授权的格式。原音轨语言标记依赖 yt-dlp 的 YouTube 元信息；低清用户提示和受限内容登录错误分类仍待后续评估。

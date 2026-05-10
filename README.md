# feed-filter

聚合多个学术 RSS/Atom 订阅源,按关键词过滤后重新发布为单个 RSS,供 Zotero 订阅。

## 输出 URL

https://<你的用户名>.github.io/feed-filter/filtered.xml

## 修改订阅源或关键词

直接编辑 `config.yaml`,提交推送,Action 自动重跑。
也可在 Actions 页面手动触发 (workflow_dispatch)。

## 调试本地运行
pip install -r requirements.txt
python filter.py

查看 `public/filtered.xml`。

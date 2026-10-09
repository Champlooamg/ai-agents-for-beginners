# InStock 连接器来源

上游版本：用户提供的 instock-master。许可：见本目录 `LICENSE`（Apache-2.0）。

本项目只实现其 `stock_hist_em.py` 中 `stock_zh_a_hist` 读取器所使用的 Eastmoney 原始日线请求口径，执行单股单日、原始价格的只读读取。完整 InStock 工程依赖数据库、浏览器/指标组件及交易适配器；这些模块未安装、未启动，也未复制到投研判断链路。

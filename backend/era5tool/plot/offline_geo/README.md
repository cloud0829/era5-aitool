# 离线底图数据目录（design-final.md §4/§12 R6）
#
# 出图底图策略：
#   1. 优先 cartopy 自带 NaturalEarth 数据（ax.coastlines()，cartopy 安装即带低清海岸线）。
#   2. 无 cartopy / 数据缺失 → 引擎自动降级纯 matplotlib（网格线，不联网）。
#
# 如需进一步离线缓存高精底图，可在此放置 shapefile/geojson 并由
# plot/engine.py 的 _new_axis 读取；当前版本不依赖任何在线下载。

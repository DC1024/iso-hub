# Python虚拟环境使用说明

## 🚀 虚拟环境设置

### 1. 创建虚拟环境
```bash
python3 -m venv venv
```

### 2. 激活虚拟环境
```bash
# macOS/Linux
source venv/bin/activate

# Windows
venv\Scripts\activate
```

### 3. 安装依赖
```bash
pip install -r requirements.txt
```

### 4. 使用脚本
```bash
# 现在可以直接使用 python 命令
python download_linux.py --list
python download_linux.py --download "Ubuntu"
```

## 🔄 日常使用

### 激活虚拟环境
每次使用前都需要激活虚拟环境：
```bash
source venv/bin/activate
```

### 退出虚拟环境
```bash
deactivate
```

### 检查虚拟环境状态
命令提示符前面有 `(venv)` 表示虚拟环境已激活。

## 📦 依赖管理

### 查看已安装的包
```bash
pip list
```

### 更新依赖
```bash
pip install --upgrade -r requirements.txt
```

### 添加新的依赖
```bash
pip install package_name
pip freeze > requirements.txt  # 更新requirements.txt
```

## 🗂️ 项目结构
```
iso/
├── venv/                    # 虚拟环境目录
├── download_linux.py        # 主脚本
├── distributions.json       # 发行版配置
├── requirements.txt         # 依赖列表
├── 使用说明.md             # 使用说明
└── venv使用说明.md         # 本文件
```

## ⚠️ 注意事项

1. **不要提交venv目录**: 将 `venv/` 添加到 `.gitignore`
2. **每次使用前激活**: 确保看到 `(venv)` 前缀
3. **依赖隔离**: 虚拟环境中的包不会影响系统Python

## 🔧 故障排除

### 虚拟环境损坏
```bash
# 删除并重新创建
rm -rf venv
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 权限问题
```bash
# 确保有执行权限
chmod +x download_linux.py
```

### 依赖冲突
```bash
# 清理并重新安装
pip uninstall -r requirements.txt -y
pip install -r requirements.txt
```

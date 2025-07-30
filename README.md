# 智能MDT多学科会诊系统

## 项目结构
```
mdt_project/
├── backend/
│   ├── models/          # 数据模型
│   ├── services/        # 业务逻辑
│   ├── app.py          # Flask应用入口
│   └── requirements.txt
├── frontend/
│   ├── src/
│   │   ├── components/  # React组件
│   │   └── App.js      # 主应用
│   └── package.json
└── README.md
```

## 功能特性
- 患者信息录入（包含主要会诊问题）
- 智能体围绕核心问题进行讨论
- 实时会诊状态显示
- 优化的可视化界面
- 最终结论生成

## 运行方式
1. 后端：`cd backend && python app.py`
2. 前端：`cd frontend && npm start`

## 使用说明

### 1. 输入病历信息
在文本框中输入患者的详细病历信息，包括：
- 主诉
- 现病史
- 既往史
- 体格检查
- 辅助检查结果
- 初步诊断

### 2. 启动会诊
点击"启动多学科会诊"按钮，系统将：
1. 分析病历内容
2. 识别相关专科
3. 并行调用各专科智能体
4. 整合分析结果
5. 生成最终结论

### 3. 查看结果
- **实时进度**：查看会诊进度和状态
- **专科意见**：查看各专科的详细分析
- **最终结论**：查看整合后的诊疗建议
- **实时日志**：查看详细的处理过程

## 系统架构

### 后端架构
```
app.py
├── Flask应用主程序
├── WebSocket实时通信
├── 多线程专科会诊
└── 智能结果整合

主要组件：
- ConsultationSession: 会诊会话管理
- controller_agent: 主控智能体
- specialist_agent: 专科智能体
- coordinator_agent: 整合智能体
```

### 前端架构
```
templates/index.html
├── 现代化UI界面
├── WebSocket客户端
├── 实时进度显示
└── 结果可视化
```

## API接口

### 1. 启动会诊
```
POST /mdt-consultation
Content-Type: application/json

{
  "medical_record": "患者病历信息"
}
```

### 2. 获取会诊状态
```
GET /consultation-status/<session_id>
```

### 3. 搜索知识库
```
POST /search-knowledge
Content-Type: application/json

{
  "query": "搜索关键词"
}
```

## WebSocket事件

### 客户端发送
- `join_session`: 加入会诊会话

### 服务器推送
- `progress_update`: 进度更新
- `specialist_result`: 专科结果
- `final_conclusion`: 最终结论

## 配置说明

在 `app.py` 中可以修改以下配置：

```python
class Config:
    API_URL = "AI API地址"
    API_KEY = "API密钥"
    MODEL_NAME = "AI模型名称"
    MAX_RETRIES = 3  # 重试次数
    TIMEOUT = 3000   # 超时时间(秒)
    MAX_CONCURRENT_SPECIALISTS = 5  # 最大并发专科数
```

## 故障排除

### 1. 依赖安装失败
```bash
# 升级pip
python -m pip install --upgrade pip

# 重新安装依赖
pip install -r requirements.txt --force-reinstall
```

### 2. 端口被占用
修改 `app.py` 中的端口号：
```python
socketio.run(app, debug=True, port=5001, host='0.0.0.0')
```

### 3. WebSocket连接失败
检查防火墙设置，确保端口5000开放。

## 开发说明

### 添加新专科
1. 在 `SPECIALISTS` 字典中添加新专科
2. 在 `SPECIALIST_DATA` 中添加前端配置
3. 在 `get_suggested_exams` 中添加检查建议

### 自定义AI模型
修改 `Config` 类中的 `API_URL` 和 `MODEL_NAME`。

## 许可证

MIT License

## 贡献

欢迎提交Issue和Pull Request来改进系统。 

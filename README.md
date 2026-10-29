# 🤖 AI Agent LangGraph - NYNA

Hệ thống AI Agent thông minh được phát triển dựa trên **LangGraph** và **LangChain**, cho phép xây dựng luồng xử lý dạng đồ thị (Graph-based Workflow), hỗ trợ duy trì trạng thái (Stateful), gọi công cụ (Tool Calling) và xử lý các tác vụ phức tạp một cách linh hoạt.

---

## 📌 Tính năng chính

- **Graph-based Workflow**: Quản lý và điều phối luồng chạy của AI Agent dạng đồ thị có trạng thái bằng LangGraph.
- **Smart Tool Calling**: Tích hợp các công cụ tự động giúp Agent tra cứu, truy xuất thông tin hoặc thực thi tác vụ bên ngoài.
- **State Management**: Quản lý ngữ cảnh và bộ nhớ hội thoại theo từng phiên làm việc (Checkpointing/Memory).
- **Linh hoạt & Dễ mở rộng**: Dễ dàng thêm mới các nút (Node), luồng rẽ nhánh (Conditional Edges), hoặc tích hợp thêm LLM models khác (OpenAI, Gemini, Ollama,...).

---

## 🛠️ Công nghệ sử dụng

- **Language**: Python 3.10+
- **Framework**: [LangGraph](https://github.com/langchain-ai/langgraph), [LangChain](https://github.com/langchain-ai/langchain)
- **LLM Models**: OpenAI / Google Gemini / Local LLMs (Ollama)
- **Environment Management**: `python-dotenv`

---

## 📁 Cấu trúc dự án

```text
AI_Agent_LangGraph_nyna/
├── config/             # Cấu hình hệ thống, API Keys
├── core/               # Định nghĩa Graph, Nodes và Edges
│   ├── state.py        # Định nghĩa AgentState
│   ├── nodes.py        # Các hàm xử lý (Nodes)
│   └── graph.py        # Khởi tạo và compile LangGraph
├── tools/              # Các công cụ mở rộng (Search, Web Browsing, DB,...)
├── main.py             # File chạy chính ứng dụng
├── requirements.txt    # Các thư viện phụ thuộc
└── README.md           # Tài liệu hướng dẫn
```

---

## 🚀 Hướng dẫn cài đặt và chạy ứng dụng

### 1. Clone repository
```bash
git clone https://github.com/NTThanh0405/AI_Agent_LangGraph_nyna.git
cd AI_Agent_LangGraph_nyna
```

### 2. Tạo môi trường ảo (Virtual Environment)
```bash
# Trên Linux/macOS
python3 -m venv venv
source venv/bin/activate

# Trên Windows
python -m venv venv
venv\Scripts\activate
```

### 3. Cài đặt các thư viện cần thiết
```bash
pip install -r requirements.txt
```

### 4. Cấu hình biến môi trường (`.env`)
Tạo một file `.env` ở thư mục gốc của dự án và điền thông tin các API key tương ứng:

```env
OPENAI_API_KEY=your_openai_api_key_here
# Hoặc nếu dùng Gemini / Anthropic / Tavily Search:
GOOGLE_API_KEY=your_google_api_key_here
TAVILY_API_KEY=your_tavily_api_key_here
```

### 5. Chạy ứng dụng
```bash
python main.py
```

---

## 📝 Ví dụ sử dụng

```python
from core.graph import app

# Đặt câu hỏi hoặc yêu cầu cho Agent
inputs = {"messages": [("user", "Hãy giúp tôi tìm hiểu về LangGraph là gì?")]}

# Chạy đồ thị
for output in app.stream(inputs):
    for key, value in output.items():
        print(f"Node '{key}':")
        print(value)
    print("\n---\n")
```

---

## 🤝 Tác giả

- **Tác giả:** [NTThanh0405](https://github.com/NTThanh0405)
- **Project:** AI Agent LangGraph NYNA

---

## 📄 Giấy phép (License)

Dự án này được phát hành dưới giấy phép [MIT License](LICENSE).
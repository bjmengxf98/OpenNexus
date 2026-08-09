# 服务端共享内存状态，避免循环导入
# uid (int) → 当前活跃会话 ID (int)
user_current_conv: dict = {}

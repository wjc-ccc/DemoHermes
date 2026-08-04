"""
Agent/memory/hot memory 保存全量加载的md文件

** markdown工作目录和markdown操作器（唯一标识、crud、retry机制、替换、去重等等）**

对于每一句加入进来的内容 都有唯一的entry_id 能够定位具体的话语
具体什么文件不管，只提供md文件的crud操作，不限于user，daily，memory等各类文件   
"""

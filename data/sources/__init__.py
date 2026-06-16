"""
数据源注册表 — 统一管理所有行情数据源
"""

DATA_SOURCE_REGISTRY = {
    'tdx': {
        'class': 'data.sources.tdx_source.TDXDataSource',
        'name': '通达信',
        'description': 'pytdx 直连行情服务器，支持5档盘口/分钟线/分笔成交',
    },
    'akshare': {
        'class': 'data.sources.akshare_source.AKShareDataSource',
        'name': 'AKShare',
        'description': '免费开源金融数据接口，数据丰富',
    },
}


def get_data_source(name: str = 'tdx', **kwargs):
    """工厂函数：获取数据源实例"""
    if name not in DATA_SOURCE_REGISTRY:
        raise ValueError(f'未知数据源: {name}，可用: {list(DATA_SOURCE_REGISTRY.keys())}')
    entry = DATA_SOURCE_REGISTRY[name]
    module_path, class_name = entry['class'].rsplit('.', 1)
    import importlib
    module = importlib.import_module(module_path)
    return getattr(module, class_name)(**kwargs)

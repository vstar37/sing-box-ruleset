# utils.py

import requests
import json
import re
import ipaddress
import pandas as pd
import yaml
import logging
import os

from config import Config
config = Config()


def read_yaml_from_url(url):
    response = requests.get(url)
    response.raise_for_status()
    yaml_data = yaml.safe_load(response.text)
    # logging.info(f"成功读取 YAML 数据 {url}")
    return yaml_data


def read_list_from_url(url):
    try:
        df = pd.read_csv(url, header=None, names=['pattern', 'address', 'other', 'other2', 'other3'])
        # logging.info(f"成功读取列表数据 {url}")
    except Exception as e:
        logging.error(f"读取 {url} 时出错：{e}")
        return pd.DataFrame(), []

    filtered_rows = []
    rules = []

    if 'AND' in df['pattern'].values:
        and_rows = df[df['pattern'].str.contains('AND', na=False)]
        for _, row in and_rows.iterrows():
            rule = {"type": "logical", "mode": "and", "rules": []}
            pattern = ",".join(row.values.astype(str))
            components = re.findall(r'\((.*?)\)', pattern)
            for component in components:
                for keyword in config.MAP_DICT.keys():
                    if keyword in component:
                        match = re.search(f'{keyword},(.*)', component)
                        if match:
                            value = match.group(1)
                            rule["rules"].append({config.MAP_DICT[keyword]: value})
            rules.append(rule)
    for index, row in df.iterrows():
        if 'AND' not in row['pattern']:
            filtered_rows.append(row)
    df_filtered = pd.DataFrame(filtered_rows, columns=['pattern', 'address', 'other', 'other2', 'other3'])
    return df_filtered, rules


def is_ipv4_or_ipv6(address):
    try:
        ipaddress.IPv4Network(address)
        return 'ipv4'
    except ValueError:
        try:
            ipaddress.IPv6Network(address)
            return 'ipv6'
        except ValueError:
            return None


def clean_json_data(data):
    """清洗 JSON 数据，移除末尾多余的逗号。"""
    cleaned_data = re.sub(r',\s*]', ']', data)  # 处理数组末尾的逗号
    cleaned_data = re.sub(r',\s*}', '}', cleaned_data)  # 处理对象末尾的逗号
    return cleaned_data


def clean_denied_domains(domains):
    """清洗 denied-remote-domains 列表中的域名并分类。"""
    cleaned_domains = {
        "domain": [],
        "domain_suffix": []
    }

    for domain in domains:
        domain = domain.strip()  # 去除前后空格
        if domain:  # 确保域名不为空
            parts = domain.split('.')
            # 判断是否为没有子域名的域名
            if len(parts) == 2:  # 例如 "0512s.com"
                cleaned_domains["domain"].append(domain)
                cleaned_domains["domain_suffix"].append("." + domain)  # 将带点的形式添加到 domain_suffix
            elif len(parts) > 2:  # 例如 "counter.packa2.cz"
                cleaned_domains["domain"].append(domain)

    return cleaned_domains


def parse_and_convert_to_dataframe(link):
    rules = []
    try:
        if link.endswith('.yaml') or link.endswith('.txt'):
            yaml_data = read_yaml_from_url(link)
            rows = []
            if not isinstance(yaml_data, str):
                items = yaml_data.get('payload', [])
            else:
                lines = yaml_data.splitlines()
                line_content = lines[0]
                items = line_content.split()
            for item in items:
                address = item.strip("'")
                if ',' not in item:
                    if is_ipv4_or_ipv6(item):
                        pattern = 'IP-CIDR'
                    else:
                        if address.startswith('+') or address.startswith('.'):
                            pattern = 'DOMAIN-SUFFIX'
                            address = address[1:]
                            if address.startswith('.'):
                                address = address[1:]
                        else:
                            pattern = 'DOMAIN'
                else:
                    pattern, address = item.split(',', 1)
                if pattern == "IP-CIDR" and "no-resolve" in address:
                    address = address.split(',', 1)[0]
                rows.append({'pattern': pattern.strip(), 'address': address.strip(), 'other': None})
            df = pd.DataFrame(rows, columns=['pattern', 'address', 'other'])
        else:
            df, rules = read_list_from_url(link)
    except Exception as e:
        logging.error(f"解析 {link} 时出错：{e}")
        return pd.DataFrame(), []

    # logging.info(f"成功解析链接 {link}")
    return df, rules


def sort_dict(obj):
    if isinstance(obj, dict):
        return {k: sort_dict(obj[k]) for k in sorted(obj)}
    elif isinstance(obj, list) and all(isinstance(elem, dict) for elem in obj):
        return sorted([sort_dict(x) for x in obj], key=lambda d: sorted(d.keys())[0])
    elif isinstance(obj, list):
        return sorted(sort_dict(x) for x in obj)
    else:
        return obj


# json去重算法
class TrieNode:
    def __init__(self):
        self.children = {}
        self.is_end = False

class Trie:
    def __init__(self):
        self.root = TrieNode()

    def insert(self, suffix):
        """ 插入 domain_suffix，确保不包含前导 . """
        suffix = suffix.lstrip('.')
        node = self.root
        for char in reversed(suffix):  # 倒序插入，方便匹配后缀
            if char not in node.children:
                node.children[char] = TrieNode()
            node = node.children[char]
        node.is_end = True

    def has_suffix(self, domain):
        """ 检查 domain 是否匹配某个完整的 domain_suffix """
        node = self.root
        domain = '.' + domain  # 加入前导点进行后缀匹配

        # 从尾部倒序遍历 domain
        for i in range(len(domain)):
            char = domain[-(i + 1)]
            if node.is_end and i != 0:  # 如果已经匹配到后缀，且 i != 0，代表匹配到完整后缀
                # 确保匹配的后缀是完整的二级域名
                if i == len(domain) - 1:  # 完全匹配
                    return True
                elif domain[-(i + 1)] == '.':  # 确保后缀结束在域名边界
                    return True
                else:
                    return False  # 如果有更多字符，且未结束，说明匹配是部分的
            if char not in node.children:
                return False
            node = node.children[char]

        # 完全匹配一个后缀时，结束条件
        return node.is_end

def filter_domains_with_trie(domains, domain_suffixes):
    """
    使用 Trie 过滤掉被 domain_suffix 覆盖的 domain。
    :param domains: 需要去重的 domain 集合
    :param domain_suffixes: domain_suffix 集合
    :return: 过滤后的 domains 和被过滤的数量
    """
    trie = Trie()

    # 统一插入 domain_suffix，去除前导 .
    for suffix in domain_suffixes:
        trie.insert(suffix)

    filtered_domains = set()  # 存储未被匹配的域名
    filtered_count = 0

    for domain in domains:
        if trie.has_suffix(domain):
            filtered_count += 1  # 被过滤的数量增加
        else:
            filtered_domains.add(domain)  # 将没有匹配到后缀的域名保留

    return filtered_domains, filtered_count

'''# 测试数据
domains = ["xp.apple.com", "example.com", "xp.apple"]
domain_suffixes = ["apple"]

final_domains, filtered_count = filter_domains_with_trie(domains, domain_suffixes)

print(f"过滤后的 domains: {final_domains}")
print(f"被过滤的数量: {filtered_count}")
'''
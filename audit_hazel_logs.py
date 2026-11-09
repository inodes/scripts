#!/usr/bin/env python3
"""Compatibility forwarder -> finance/audit_hazel_logs.py"""
import os
import sys

TARGET = os.path.join(os.path.dirname(os.path.abspath(__file__)), "finance", "audit_hazel_logs.py")
os.execv(sys.executable, [sys.executable, TARGET] + sys.argv[1:])

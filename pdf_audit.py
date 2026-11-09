#!/usr/bin/env python3
"""Compatibility forwarder -> finance/pdf_audit.py"""
import os
import sys

TARGET = os.path.join(os.path.dirname(os.path.abspath(__file__)), "finance", "pdf_audit.py")
os.execv(sys.executable, [sys.executable, TARGET] + sys.argv[1:])

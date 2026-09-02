# -*- coding: utf-8 -*-

################################################################################
## Form generated from reading UI file 'preview_dialog.ui'
##
## Created by: Qt User Interface Compiler version 6.11.1
##
## WARNING! All changes made in this file will be lost when recompiling UI file!
################################################################################

from PySide6.QtCore import (QCoreApplication, QDate, QDateTime, QLocale,
    QMetaObject, QObject, QPoint, QRect,
    QSize, QTime, QUrl, Qt)
from PySide6.QtGui import (QBrush, QColor, QConicalGradient, QCursor,
    QFont, QFontDatabase, QGradient, QIcon,
    QImage, QKeySequence, QLinearGradient, QPainter,
    QPalette, QPixmap, QRadialGradient, QTransform)
from PySide6.QtWidgets import (QApplication, QGridLayout, QHBoxLayout, QLabel,
    QLayout, QPushButton, QSizePolicy, QSpacerItem,
    QVBoxLayout, QWidget)

class Ui_Form(object):
    def setupUi(self, Form):
        if not Form.objectName():
            Form.setObjectName(u"Form")
        Form.resize(930, 859)
        self.verticalLayout_2 = QVBoxLayout(Form)
        self.verticalLayout_2.setObjectName(u"verticalLayout_2")
        self.horizontalLayout_2 = QHBoxLayout()
        self.horizontalLayout_2.setObjectName(u"horizontalLayout_2")
        self.label = QLabel(Form)
        self.label.setObjectName(u"label")

        self.horizontalLayout_2.addWidget(self.label)

        self.verticalLayout = QVBoxLayout()
        self.verticalLayout.setObjectName(u"verticalLayout")
        self.verticalLayout.setSizeConstraint(QLayout.SizeConstraint.SetMinAndMaxSize)
        self.label_2 = QLabel(Form)
        self.label_2.setObjectName(u"label_2")
        self.label_2.setStyleSheet(u"background-color: #fff2bc;\n"
"border:2px solid black;\n"
"padding:8px;\n"
"color:#593800;")

        self.verticalLayout.addWidget(self.label_2)

        self.horizontalLayout = QHBoxLayout()
        self.horizontalLayout.setObjectName(u"horizontalLayout")
        self.pushButton = QPushButton(Form)
        self.pushButton.setObjectName(u"pushButton")
        self.pushButton.setMinimumSize(QSize(0, 40))

        self.horizontalLayout.addWidget(self.pushButton)

        self.pushButton_2 = QPushButton(Form)
        self.pushButton_2.setObjectName(u"pushButton_2")
        self.pushButton_2.setMinimumSize(QSize(0, 40))

        self.horizontalLayout.addWidget(self.pushButton_2)


        self.verticalLayout.addLayout(self.horizontalLayout)

        self.gridLayout = QGridLayout()
        self.gridLayout.setObjectName(u"gridLayout")
        self.pushButton_4 = QPushButton(Form)
        self.pushButton_4.setObjectName(u"pushButton_4")
        self.pushButton_4.setMinimumSize(QSize(0, 50))
        self.pushButton_4.setMaximumSize(QSize(16777215, 16777215))

        self.gridLayout.addWidget(self.pushButton_4, 1, 0, 1, 1)

        self.pushButton_5 = QPushButton(Form)
        self.pushButton_5.setObjectName(u"pushButton_5")
        self.pushButton_5.setMinimumSize(QSize(0, 50))
        self.pushButton_5.setMaximumSize(QSize(16777215, 16777215))

        self.gridLayout.addWidget(self.pushButton_5, 1, 2, 1, 1)

        self.pushButton_6 = QPushButton(Form)
        self.pushButton_6.setObjectName(u"pushButton_6")
        self.pushButton_6.setMinimumSize(QSize(0, 50))
        self.pushButton_6.setMaximumSize(QSize(16777215, 16777215))

        self.gridLayout.addWidget(self.pushButton_6, 2, 1, 1, 1)

        self.pushButton_3 = QPushButton(Form)
        self.pushButton_3.setObjectName(u"pushButton_3")
        self.pushButton_3.setMinimumSize(QSize(0, 50))
        self.pushButton_3.setMaximumSize(QSize(16777215, 16777215))

        self.gridLayout.addWidget(self.pushButton_3, 0, 1, 1, 1)


        self.verticalLayout.addLayout(self.gridLayout)

        self.pushButton_7 = QPushButton(Form)
        self.pushButton_7.setObjectName(u"pushButton_7")
        self.pushButton_7.setMinimumSize(QSize(0, 40))

        self.verticalLayout.addWidget(self.pushButton_7)

        self.verticalSpacer = QSpacerItem(20, 40, QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Expanding)

        self.verticalLayout.addItem(self.verticalSpacer)


        self.horizontalLayout_2.addLayout(self.verticalLayout)

        self.horizontalLayout_2.setStretch(0, 99)
        self.horizontalLayout_2.setStretch(1, 1)

        self.verticalLayout_2.addLayout(self.horizontalLayout_2)

        self.pushButton_8 = QPushButton(Form)
        self.pushButton_8.setObjectName(u"pushButton_8")
        self.pushButton_8.setMinimumSize(QSize(0, 40))

        self.verticalLayout_2.addWidget(self.pushButton_8)


        self.retranslateUi(Form)

        QMetaObject.connectSlotsByName(Form)
    # setupUi

    def retranslateUi(self, Form):
        Form.setWindowTitle(QCoreApplication.translate("Form", u"\u8ba1\u5206\u677f\u9884\u89c8", None))
        self.label.setText("")
        self.label_2.setText(QCoreApplication.translate("Form", u"<html><head/><body><p>\u5f53\u753b\u9762\u8bfb\u53d6\u4e0d\u51c6\u786e\u65f6\uff0c\u53ef\u5728\u6b64\u754c\u9762\u8fdb\u884c\u8c03\u6574</p><p>\u786e\u4fdd\u79ef\u5206\u677f\u753b\u9762\u5bf9\u9f50</p></body></html>", None))
        self.pushButton.setText(QCoreApplication.translate("Form", u"\u653e\u5927", None))
        self.pushButton_2.setText(QCoreApplication.translate("Form", u"\u7f29\u5c0f", None))
        self.pushButton_4.setText(QCoreApplication.translate("Form", u"\u5de6\u79fb", None))
        self.pushButton_5.setText(QCoreApplication.translate("Form", u"\u53f3\u79fb", None))
        self.pushButton_6.setText(QCoreApplication.translate("Form", u"\u4e0b\u79fb", None))
        self.pushButton_3.setText(QCoreApplication.translate("Form", u"\u4e0a\u79fb", None))
        self.pushButton_7.setText(QCoreApplication.translate("Form", u"\u6062\u590d\u9ed8\u8ba4", None))
        self.pushButton_8.setText(QCoreApplication.translate("Form", u"\u5b8c\u6210", None))
    # retranslateUi

